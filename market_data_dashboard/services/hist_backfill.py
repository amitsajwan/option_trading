"""Historical snapshot backfill via Dhan /charts/rollingoption (Phase 1 depth).

2026-07-10 discovery: Dhan's rollingoption endpoint serves 1-min premium/IV/OI
for ANY strike back to (at least) Nov 2024 — expired contracts resolved
server-side. This overturns the old "no historical option-chain data" verdict
and unlocks the full Seller v2 Phase 1 window.

Pipeline (all existing pieces):
  ingestion_app /api/v1/historical/day/{instrument}?date=D&strikes=12
    -> build_snapshots_from_dhan_data()  (runtime snapshot format)
    -> Mongo `phase1_market_snapshots_hist`  (SEPARATE collection — never mix
       backfilled data with the live-collected `phase1_market_snapshots`)
    -> strategy_app.seller.replay --coll phase1_market_snapshots_hist

Run inside the dashboard container (has this package + pymongo + network):
  python -m market_data_dashboard.services.hist_backfill \
      --from 2024-11-01 --to 2026-05-23 [--instrument BANKNIFTY]

Idempotent per day (existing day docs are deleted before insert). Weekends are
skipped; holidays return 0 index bars and are skipped automatically. Throttled
~1 day/min to respect Dhan rate limits (50+ rollingoption calls per day built).
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

from .dhan_replay_builder import build_snapshots_from_dhan_data
from snapshot_app.core.live_velocity_state import (
    LiveVelocityAccumulator,
    make_mongo_context_provider,
)
from snapshot_app.core.market_snapshot_contract import validate_market_snapshot

_INGESTION = os.getenv("INGESTION_BASE", "http://ingestion_app:8004")
_COLL = "phase1_market_snapshots_hist"


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """weekday: Mon=0 .. Sun=6."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def nifty_weekly_expiry(d: date) -> date:
    """NIFTY weekly expiry on/after d. Thursday historically; NSE moved index
    weekly expiry to Tuesday ~Sep-2025 (scrip master shows Tuesdays now).
    VERIFY against contract notes if DTE-sensitive results look off."""
    target = 3 if d < date(2025, 9, 1) else 1  # Thu -> Tue
    exp = d + timedelta(days=(target - d.weekday()) % 7)
    return exp


def sensex_expiry(d: date) -> date:
    """SENSEX (BSE) weekly expiry on/after d. Confirmed live 2026-08-07 via
    scrip master: every listed SENSEX expiry (7 near-term weeklies plus
    monthly/quarterly further out, through 2031) lands on a Thursday with
    zero exceptions -- no NSE-style mid-year weekday cutover to track here,
    unlike nifty_weekly_expiry. VERIFY against a live scrip-master pull if
    BSE's SEBI-mandated weekly-expiry day ever changes."""
    target = 3  # Thursday
    exp = d + timedelta(days=(target - d.weekday()) % 7)
    return exp


def bn_monthly_expiry(d: date) -> date:
    """BANKNIFTY monthly expiry for the month containing/after d.

    Exchange expiry-day changed over the window (VERIFY against contract notes
    if DTE-sensitive results look off):
      Nov-2024 .. Mar-2025 : last Wednesday   (weekly BN discontinued Nov-2024)
      Apr-2025 .. Aug-2025 : last Thursday    (NSE moved index F&O expiry)
      Sep-2025 .. now      : last Tuesday     (SEBI Tue/Thu rule; NSE chose Tue —
                                               2026-07-28 IS the last Tuesday ✓)
    """
    def _day_for(y: int, m: int) -> int:
        if (y, m) <= (2025, 3):
            return 2  # Wednesday
        if (y, m) <= (2025, 8):
            return 3  # Thursday
        return 1      # Tuesday

    exp = _last_weekday_of_month(d.year, d.month, _day_for(d.year, d.month))
    if exp < d:  # already past this month's expiry -> next month's
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        exp = _last_weekday_of_month(y, m, _day_for(y, m))
    return exp


def _coll_for(instrument: str) -> str:
    inst = instrument.strip().upper()
    return _COLL if inst == "BANKNIFTY" else f"{_COLL}_{inst.lower()}"


def _enrich_for_seller(snapshots: list[dict], trade_date: str, instrument: str = "BANKNIFTY") -> None:
    """Gaps in the replay builder's output that the seller needs:
    - futures_bar.fut_close ('close' in builder; SnapshotAccessor reads 'fut_close')
    - session_context.days_to_expiry (None in builder; DTE drives seller exits)
    - per-minute strike gaps: rollingoption has minutes where one side didn't
      trade -> that side's ltp is None -> the chain-shift price fallback quotes a
      NEIGHBOR strike for it, making short and hedge price identical (est credit
      0, seller refuses every entry — 2026-07-10 hist smoke). Forward-fill each
      strike's fields from its own last known value within the day.
    - EMA/compression family + max_pain/ATM-OI aggregates (2026-07-11 A4 study):
      the live builder computes these statefully (runtime_features) but this
      per-day path never did -> replay/study scores were dampened vs live and
      the direction lever was blind on hist data. Enriched here via the SAME
      canonical modules the training view uses (no drift)."""
    d = date.fromisoformat(trade_date)
    # Instrument-keyed, NOT cadence-keyed (fixed 2026-08-07 for SENSEX): a
    # cadence binary (monthly->bn_monthly_expiry, else->nifty_weekly_expiry)
    # is exactly the bug shape that gave FINNIFTY (monthly per registry)
    # NIFTY's WEEKLY expiry on every backfilled snapshot -- wrong
    # days_to_expiry/is_expiry_day/expiry on all 359 days of its first clean
    # backfill (found 2026-08-04 sweep). Picking the "right" existing
    # function by cadence alone isn't safe either: SENSEX is weekly cadence
    # like NIFTY, but nifty_weekly_expiry's NSE-specific Thu->Tue cutover
    # (Sep-2025) does not apply to BSE (confirmed live: SENSEX is Thursday,
    # unconditionally). DTE drives seller exits and is a model feature.
    _EXPIRY_FN = {
        "BANKNIFTY": bn_monthly_expiry,
        "NIFTY": nifty_weekly_expiry,
        "FINNIFTY": bn_monthly_expiry,
        "SENSEX": sensex_expiry,
    }
    inst_u = instrument.strip().upper()
    if inst_u not in _EXPIRY_FN:
        raise KeyError(
            f"no expiry function registered for {inst_u!r} in hist_backfill._EXPIRY_FN "
            "-- add one (never fall back to cadence-matching, see comment above)"
        )
    exp = _EXPIRY_FN[inst_u](d)
    dte = (exp - d).days
    last: dict[tuple[int, str], float] = {}   # (strike, field) -> last non-None
    for s in snapshots:
        fb = s.get("futures_bar") or {}
        if "fut_close" not in fb:
            fb["fut_close"] = fb.get("close")
        sc = s.get("session_context") or {}
        sc["days_to_expiry"] = dte
        sc["is_expiry_day"] = dte == 0
        sc["expiry"] = exp.isoformat()
        for row in (s.get("strikes") or []):
            k = row.get("strike")
            if k is None:
                continue
            k = int(k)
            for f in ("ce_ltp", "pe_ltp", "ce_iv", "pe_iv", "ce_oi", "pe_oi"):
                if row.get(f) is not None:
                    last[(k, f)] = row[f]
                elif (k, f) in last:
                    row[f] = last[(k, f)]
    enrich_day_ema_and_aggregates(snapshots)


def enrich_day_ema_and_aggregates(snapshots: list[dict]) -> None:
    """Per-day EMA/compression family (canonical modules, mirrors the training
    view's _enrich_ema_family) + max_pain / ATM-OI-change aggregates derived
    from the stored chain. Values land at snapshot payload ROOT (build_feature_
    row's root-scalar fill picks them up) and in chain_aggregates/atm_options
    (SnapshotAccessor paths the direction lever reads). Idempotent — only fills
    missing/None. Callable standalone for re-enriching already-stored days."""
    import math

    import pandas as pd

    from snapshot_app.core.compression_features import add_compression_features
    from snapshot_app.core.feature_engine import _ema

    df = pd.DataFrame({
        "close": [(s.get("futures_bar") or {}).get("fut_close") for s in snapshots],
        "high": [(s.get("futures_bar") or {}).get("high") for s in snapshots],
        "low": [(s.get("futures_bar") or {}).get("low") for s in snapshots],
        "volume": [(s.get("futures_bar") or {}).get("volume") for s in snapshots],
    })
    close = df["close"].astype(float)
    for span in (9, 21, 50):
        df[f"ema_{span}"] = _ema(close, span)
    df["day_high"] = df["high"].cummax()
    df["day_low"] = df["low"].cummin()
    add_compression_features(df)
    nz = close.replace(0.0, float("nan"))
    for span in (9, 21, 50):
        df[f"ema_{span}_slope"] = df[f"ema_{span}"].diff() / nz
    # Session VWAP -- cumulative volume-weighted close from the day's open.
    vol = df["volume"].astype(float).fillna(0.0)
    cum_vol = vol.cumsum()
    df["vwap"] = (close * vol).cumsum() / cum_vol.replace(0.0, float("nan"))
    df["price_vs_vwap"] = (close - df["vwap"]) / df["vwap"].replace(0.0, float("nan"))

    oi_hist: list[dict] = []
    for i, s in enumerate(snapshots):
        for col in df.columns:
            if col in ("close", "high", "low", "volume"):
                continue
            cur = s.get(col)
            if cur is None or (isinstance(cur, float) and math.isnan(cur)):
                v = df[col].iloc[i]
                if not pd.isna(v):
                    s[col] = float(v)
        # Same EMA/slope/vwap values, ALSO under futures_derived -- live's
        # build_market_snapshot puts them there (mss3 block); the root-level
        # copy above is kept as-is for build_feature_row's existing root-scalar
        # fallback, this just stops futures_derived.ema_9 etc from being
        # permanently missing against the live schema contract.
        fd = s.setdefault("futures_derived", {})
        for col in ("ema_9", "ema_21", "ema_50", "ema_9_slope", "ema_21_slope", "ema_50_slope", "vwap", "price_vs_vwap"):
            if fd.get(col) is None:
                v = df[col].iloc[i]
                if not pd.isna(v):
                    fd[col] = float(v)
        oi_map = {int(r["strike"]): (float(r.get("ce_oi") or 0), float(r.get("pe_oi") or 0))
                  for r in (s.get("strikes") or []) if r.get("strike")}
        oi_hist.append(oi_map)
        if len(oi_map) >= 10:
            ca = s.setdefault("chain_aggregates", {})
            if not ca.get("max_pain"):
                ca["max_pain"] = min(
                    sorted(oi_map),
                    key=lambda S: sum(c * max(0, S - K) + q * max(0, K - S)
                                      for K, (c, q) in oi_map.items()))
            fut_close = (s.get("futures_bar") or {}).get("fut_close")
            max_pain = ca.get("max_pain")
            if ca.get("distance_to_max_pain_pct") is None and fut_close and max_pain:
                ca["distance_to_max_pain_pct"] = (float(fut_close) - float(max_pain)) / float(max_pain)
        atm = (s.get("chain_aggregates") or {}).get("atm_strike")
        if atm and i >= 30 and atm in oi_map and atm in oi_hist[i - 30]:
            ao = s.setdefault("atm_options", {})
            if ao.get("atm_ce_oi_change_30m") is None:
                ao["atm_ce_oi_change_30m"] = oi_map[atm][0] - oi_hist[i - 30][atm][0]
                ao["atm_pe_oi_change_30m"] = oi_map[atm][1] - oi_hist[i - 30][atm][1]


def enrich_day_mtf_opening_iv(snapshots: list[dict]) -> None:
    """Fill mtf_derived, opening_range, and the per-bar half of iv_derived
    (iv_skew/iv_skew_dir/iv_expiry_type) -- the schema blocks the original
    hist_backfill builder never produced at all (found 2026-08-11 auditing
    why a month-long replay against phase1_market_snapshots_hist failed
    market_snapshot_contract validation). Reuses the SAME pure functions the
    live builder uses (snapshot_app.core.market_snapshot._compute_mtf_block,
    _bars_since_first_true) so this can't drift from live's definition again.

    Must run AFTER _enrich_for_seller() (needs session_context.is_expiry_day)
    and idempotent -- only fills currently-None values. iv_percentile/
    iv_regime and session_levels are filled by separate cross-day passes
    (iv_percentile_pass, session_levels_pass) that need chronological state
    across multiple days already in Mongo, not available at single-day
    build time.
    """
    import pandas as pd

    from snapshot_app.core.market_snapshot import _bars_since_first_true, _compute_mtf_block

    if not snapshots:
        return

    df = pd.DataFrame({
        "timestamp": [s.get("timestamp") for s in snapshots],
        "open": [(s.get("futures_bar") or {}).get("fut_open") for s in snapshots],
        "high": [(s.get("futures_bar") or {}).get("fut_high") for s in snapshots],
        "low": [(s.get("futures_bar") or {}).get("fut_low") for s in snapshots],
        "close": [(s.get("futures_bar") or {}).get("fut_close") for s in snapshots],
        "volume": [(s.get("futures_bar") or {}).get("fut_volume") for s in snapshots],
    })
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    for i, s in enumerate(snapshots):
        mtf = s.setdefault("mtf_derived", {})
        if mtf.get("mtf_aligned") is None:
            mtf.update(_compute_mtf_block(df.iloc[: i + 1]))

    # Opening range: fixed for the whole day once 9:15-9:30 has passed.
    open_window = df[(df["timestamp"].dt.hour == 9) & (df["timestamp"].dt.minute >= 15) & (df["timestamp"].dt.minute < 30)]
    orh = float(open_window["high"].max()) if len(open_window) else None
    orl = float(open_window["low"].min()) if len(open_window) else None
    or_width = (orh - orl) if (orh is not None and orl is not None) else None
    breakout_up = (df["close"] > orh) if orh is not None else pd.Series(False, index=df.index)
    breakout_down = (df["close"] < orl) if orl is not None else pd.Series(False, index=df.index)

    for i, s in enumerate(snapshots):
        orr = s.setdefault("opening_range", {})
        if orr.get("orh") is not None:
            continue  # already filled (idempotent re-run)
        fut_close = (s.get("futures_bar") or {}).get("fut_close")
        minutes_since_open = (s.get("session_context") or {}).get("minutes_since_open")
        price_vs_orh = ((fut_close - orh) / orh) if (fut_close and orh) else None
        price_vs_orl = ((fut_close - orl) / orl) if (fut_close and orl) else None
        or_width_pct = (or_width / fut_close) if (or_width is not None and fut_close) else None
        bars_since_up = _bars_since_first_true(breakout_up.iloc[: i + 1]) if orh is not None else None
        bars_since_down = _bars_since_first_true(breakout_down.iloc[: i + 1]) if orl is not None else None
        orr.update({
            "orh": orh,
            "orl": orl,
            "or_width": or_width,
            "or_width_pct": or_width_pct,
            "price_vs_orh": price_vs_orh,
            "price_vs_orl": price_vs_orl,
            "opening_range_ready": bool(minutes_since_open is not None and minutes_since_open >= 15),
            "orh_broken": bool(orh is not None and breakout_up.iloc[: i + 1].any()),
            "orl_broken": bool(orl is not None and breakout_down.iloc[: i + 1].any()),
            "bars_since_or_break_up": bars_since_up,
            "bars_since_or_break_down": bars_since_down,
        })

        iv = s.setdefault("iv_derived", {})
        if iv.get("iv_skew") is None:
            atm = s.get("atm_options") or {}
            ce_iv, pe_iv = atm.get("atm_ce_iv"), atm.get("atm_pe_iv")
            iv_skew = (ce_iv - pe_iv) if (ce_iv is not None and pe_iv is not None) else None
            if iv_skew is None:
                iv_skew_dir = None
            elif iv_skew < -0.005:
                iv_skew_dir = "PUT_FEAR"
            elif iv_skew > 0.005:
                iv_skew_dir = "CALL_GREED"
            else:
                iv_skew_dir = "NEUTRAL"
            is_expiry_day = (s.get("session_context") or {}).get("is_expiry_day")
            iv.update({
                "iv_skew": iv_skew,
                "iv_skew_dir": iv_skew_dir,
                "iv_expiry_type": "EXPIRY_DAY" if is_expiry_day else "NON_EXPIRY",
            })


def _quality_check(snapshots: list[dict]) -> tuple[bool, dict]:
    """Per-day data-quality gate (2026-07-10 clean-rebuild mandate): a day that
    fails does NOT enter Mongo. Checks target the failure modes we actually
    shipped this week: ATM-clone ladders, one-sided chains, missing marks.
    """
    rep: dict = {"bars": len(snapshots)}
    if len(snapshots) < 300:
        rep["fail"] = "too_few_bars"
        return False, rep
    mid = snapshots[len(snapshots) // 2]
    # BUG (found 2026-07-26 onboarding FINNIFTY, also reachable by
    # BANKNIFTY/NIFTY on any illiquid strike): `if r.get("ce_ltp")` treats a
    # float NaN as truthy (only None/0/"" are falsy in Python), so a strike
    # with ce_ltp=nan was NOT excluded here -- it then flowed into the
    # monotonicity check below, where `nan >= x` and `nan <= x` are ALWAYS
    # False, registering as a fake "non_monotone_chain" violation that has
    # nothing to do with real chain shape. _valid() explicitly rejects NaN.
    def _valid(v: object) -> bool:
        return v is not None and not (isinstance(v, float) and math.isnan(v))
    rows = [r for r in (mid.get("strikes") or []) if _valid(r.get("ce_ltp")) and _valid(r.get("pe_ltp"))]
    rep["mid_strikes_priced"] = len(rows)
    if len(rows) < 12:
        rep["fail"] = "thin_chain"
        return False, rep
    # anti-clone: premiums must be overwhelmingly distinct across strikes
    ce = [round(float(r["ce_ltp"]), 2) for r in rows]
    rep["ce_distinct_frac"] = round(len(set(ce)) / len(ce), 2)
    if rep["ce_distinct_frac"] < 0.8:
        rep["fail"] = "clone_ladder"
        return False, rep
    # monotonicity: CE falls / PE rises with strike (>=85% of adjacent pairs)
    rows.sort(key=lambda r: r["strike"])
    ce_ok = sum(1 for a, b in zip(rows, rows[1:]) if float(a["ce_ltp"]) >= float(b["ce_ltp"]))
    pe_ok = sum(1 for a, b in zip(rows, rows[1:]) if float(a["pe_ltp"]) <= float(b["pe_ltp"]))
    rep["ce_monotone_frac"] = round(ce_ok / (len(rows) - 1), 2)
    rep["pe_monotone_frac"] = round(pe_ok / (len(rows) - 1), 2)
    if rep["ce_monotone_frac"] < 0.85 or rep["pe_monotone_frac"] < 0.85:
        rep["fail"] = "non_monotone_chain"
        return False, rep
    # coverage: fut_close on every bar; priced strikes on >=95% of bars
    miss_fut = sum(1 for s in snapshots if not (s.get("futures_bar") or {}).get("fut_close"))
    rep["missing_fut_close"] = miss_fut
    if miss_fut > 0:
        rep["fail"] = "missing_fut_close"
        return False, rep
    return True, rep


def _inject_velocity(mongo_db, snapshots: list, instrument: str) -> None:
    """Port of LiveMarketSnapshotBuilder's per-bar velocity injection (2026-07-31 fix).

    Historical backfill never ran LiveVelocityAccumulator, so every backfilled
    day was missing snapshot["velocity_enrichment"] entirely (confirmed: live
    snapshots have it from ~9:18 onward, backfilled ones had zero vel_* fields
    at ANY time of day, not just before the old 11:30 anchor -- this is a real
    gap, not the accumulator's legitimate pre-warmup NaN window). ~19 of a
    47-feature entry-model bundle are vel_*, so this silently degraded any
    model trained or backtested against phase1_market_snapshots_hist{,_*} to
    ~40% real features, median-imputed for the rest. Mutates snapshots in place.
    Requires snapshots sorted ascending by timestamp (same-day, single
    accumulator instance replicates the live per-day state-reset boundary).
    """
    snapshots.sort(key=lambda s: s.get("timestamp") or "")
    acc = LiveVelocityAccumulator(
        context_provider=make_mongo_context_provider(
            mongo_db, collections=(_coll_for(instrument),)
        )
    )
    for i, snap in enumerate(snapshots):
        snapshots[i] = acc.process(snap)


def backfill_day(mongo_db, instrument: str, trade_date: str, strikes: int = 10) -> int:
    r = requests.get(
        f"{_INGESTION}/api/v1/historical/day/{instrument}",
        params={"date": trade_date, "strikes": strikes, "interval": "1"},
        timeout=600,
    )
    r.raise_for_status()
    raw = r.json()
    if not raw.get("index_bars"):
        return 0  # holiday / no session
    snapshots = build_snapshots_from_dhan_data(raw)
    _enrich_for_seller(snapshots, trade_date, instrument)  # also runs enrich_day_ema_and_aggregates
    enrich_day_mtf_opening_iv(snapshots)
    _inject_velocity(mongo_db, snapshots, instrument)
    ok, report = _quality_check(snapshots)
    report.update({"instrument": instrument, "trade_date": trade_date,
                   "verified_at": datetime.now(timezone.utc).isoformat(), "passed": ok})
    mongo_db["dataset_manifests"].update_one(
        {"_id": f"{_coll_for(instrument)}:{trade_date}"}, {"$set": report}, upsert=True)
    if not ok:
        print(f"{trade_date} QUALITY_FAIL {report.get('fail')} — day NOT ingested", flush=True)
        return -1
    # Schema-contract gate (added 2026-08-11): the original builder silently
    # produced snapshots missing 5+ top-level blocks and dozens of sub-fields
    # for almost a month before anyone noticed, because nothing here ever
    # called the SAME validator the live/replay paths already use. Values can
    # legitimately be None (see per-field comments above/in
    # enrich_day_mtf_opening_iv) -- this only checks required KEYS are
    # present, matching validate_market_snapshot's own semantics.
    schema_errors = 0
    for s in snapshots:
        rep = validate_market_snapshot(s, raise_on_error=False)
        schema_errors += rep["error_count"]
    if schema_errors:
        print(f"{trade_date} SCHEMA_FAIL {schema_errors} field errors across "
              f"{len(snapshots)} bars — day NOT ingested", flush=True)
        mongo_db["dataset_manifests"].update_one(
            {"_id": f"{_coll_for(instrument)}:{trade_date}"},
            {"$set": {"schema_errors": schema_errors, "passed": False}})
        return -1
    coll = mongo_db[_coll_for(instrument)]
    coll.delete_many({"trade_date_ist": trade_date})  # idempotent per day
    docs = [{
        "event_type": "hist_backfill",
        "snapshot_id": s.get("snapshot_id"),
        "instrument": instrument,
        "timestamp": s.get("timestamp"),
        "trade_date_ist": trade_date,
        "payload": {"snapshot": s},
    } for s in snapshots]
    if docs:
        coll.insert_many(docs)
    return len(docs)


def iv_percentile_pass(mongo_db, d_from: str, d_to: str, window_days: int = 20, coll_name: str = _COLL) -> None:
    """Second pass: fill iv_derived.iv_percentile per snapshot, MIRRORING the
    live algorithm exactly (snapshot_app/core/market_snapshot.py ~1682):
    per-bar rolling deque maxlen=30000 (~80 sessions), SPLIT expiry-day vs
    non-expiry histories, percentile computed against history BEFORE appending
    the current bar. 2026-07-11: the previous 20-day pooled version produced a
    different IV series than live → different entry days → replay and live
    were not comparable (3 vs 8 entries on the same window)."""
    from bisect import bisect_right, insort
    from collections import deque

    coll = mongo_db[coll_name]
    days = sorted(coll.distinct("trade_date_ist", {"trade_date_ist": {"$gte": d_from, "$lte": d_to}}))
    hist = {False: deque(maxlen=30000), True: deque(maxlen=30000)}
    sorted_hist = {False: [], True: []}
    from pymongo import UpdateOne
    for day in days:
        ops = []
        for doc in coll.find({"trade_date_ist": day},
                             {"payload.snapshot.atm_options.atm_iv": 1,
                              "payload.snapshot.session_context.is_expiry_day": 1},
                             sort=[("timestamp", 1)]):
            snap = (doc.get("payload") or {}).get("snapshot") or {}
            v = (snap.get("atm_options") or {}).get("atm_iv")
            if not v:
                continue
            v = float(v)
            exp = bool((snap.get("session_context") or {}).get("is_expiry_day"))
            sh, dq = sorted_hist[exp], hist[exp]
            if sh:  # percentile vs history BEFORE append (live order)
                pct = 100.0 * bisect_right(sh, v) / len(sh)
                # iv_regime thresholds mirror market_snapshot.py's live
                # classification exactly (<40 CHEAP, <=75 NEUTRAL, else EXPENSIVE).
                if pct < 40.0:
                    regime = "CHEAP"
                elif pct <= 75.0:
                    regime = "NEUTRAL"
                else:
                    regime = "EXPENSIVE"
                ops.append(UpdateOne({"_id": doc["_id"]},
                                     {"$set": {
                                         "payload.snapshot.iv_derived.iv_percentile": round(pct, 1),
                                         "payload.snapshot.iv_derived.iv_regime": regime,
                                     }}))
            if len(dq) == dq.maxlen:  # evict oldest from the sorted view too
                old = dq[0]
                i = bisect_right(sh, old) - 1
                if i >= 0:
                    sh.pop(i)
            dq.append(v)
            insort(sh, v)
        if ops:
            coll.bulk_write(ops)
        print(f"iv_pass {day} hist={len(sorted_hist[False])}/{len(sorted_hist[True])}", flush=True)
    print("IV_PASS_DONE", flush=True)


def session_levels_pass(mongo_db, d_from: str, d_to: str, coll_name: str = _COLL) -> None:
    """Second pass: fill session_levels (prev_day_high/low/close, week_high/
    low, overnight_gap) -- needs the prior trading day(s)' OHLC, which isn't
    available at single-day backfill_day() time. Mirrors iv_percentile_pass's
    shape: walk days chronologically, carry a small trailing-day summary
    forward, $set onto every bar of the current day. The FIRST day(s) in a
    range have no prior day within it and are left None -- same cold-start
    shape as the live IV deque's first warm-up window."""
    from pymongo import UpdateOne

    coll = mongo_db[coll_name]
    days = sorted(coll.distinct("trade_date_ist", {"trade_date_ist": {"$gte": d_from, "$lte": d_to}}))
    day_summaries: dict[str, dict] = {}  # trade_date -> {high, low, close}

    for day in days:
        docs = list(coll.find(
            {"trade_date_ist": day},
            {"payload.snapshot.futures_bar.fut_high": 1,
             "payload.snapshot.futures_bar.fut_low": 1,
             "payload.snapshot.futures_bar.fut_close": 1,
             "payload.snapshot.futures_bar.fut_open": 1},
            sort=[("timestamp", 1)],
        ))
        if not docs:
            continue

        prior_days = [d for d in days if d < day and d in day_summaries]
        trailing = prior_days[-5:]
        prev_day = prior_days[-1] if prior_days else None
        prev = day_summaries.get(prev_day) if prev_day else None
        week_high = max((day_summaries[d]["high"] for d in trailing if day_summaries[d]["high"] is not None), default=None)
        week_low = min((day_summaries[d]["low"] for d in trailing if day_summaries[d]["low"] is not None), default=None)
        today_open = None
        for doc in docs:
            fb = ((doc.get("payload") or {}).get("snapshot") or {}).get("futures_bar") or {}
            if fb.get("fut_open") is not None:
                today_open = fb["fut_open"]
                break
        overnight_gap = (
            (today_open - prev["close"]) / prev["close"]
            if (prev and prev.get("close") and today_open is not None) else None
        )

        ops = []
        day_high = day_low = None
        day_close = None
        for doc in docs:
            fb = ((doc.get("payload") or {}).get("snapshot") or {}).get("futures_bar") or {}
            h, l, c = fb.get("fut_high"), fb.get("fut_low"), fb.get("fut_close")
            if h is not None:
                day_high = h if day_high is None else max(day_high, h)
            if l is not None:
                day_low = l if day_low is None else min(day_low, l)
            if c is not None:
                day_close = c
            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {
                "payload.snapshot.session_levels.prev_day_high": prev["high"] if prev else None,
                "payload.snapshot.session_levels.prev_day_low": prev["low"] if prev else None,
                "payload.snapshot.session_levels.prev_day_close": prev["close"] if prev else None,
                "payload.snapshot.session_levels.week_high": week_high,
                "payload.snapshot.session_levels.week_low": week_low,
                "payload.snapshot.session_levels.overnight_gap": overnight_gap,
            }}))
        if ops:
            coll.bulk_write(ops)
        day_summaries[day] = {"high": day_high, "low": day_low, "close": day_close}
        print(f"session_levels_pass {day} prev={prev_day}", flush=True)
    print("SESSION_LEVELS_PASS_DONE", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--instrument", default="BANKNIFTY")
    ap.add_argument("--strikes", type=int, default=10)  # ingestion endpoint caps le=10 (ATM±10 = 21 strikes; seller legs live within ±5)
    ap.add_argument("--pause", type=float, default=20.0, help="seconds between days (rate limit)")
    ap.add_argument("--iv-pass", action="store_true", help="only run the iv_percentile + session_levels second passes")
    ap.add_argument("--skip-second-passes", action="store_true",
                     help="skip the automatic iv_percentile_pass/session_levels_pass at the end of a range backfill")
    args = ap.parse_args()

    from pymongo import MongoClient
    db = MongoClient(os.getenv("MONGO_HOST", "mongo"),
                     int(os.getenv("MONGO_PORT", "27017") or 27017))[
        os.getenv("MONGO_DB", "trading_ai")]

    if args.iv_pass:
        iv_percentile_pass(db, args.d_from, args.d_to, coll_name=_coll_for(args.instrument))
        session_levels_pass(db, args.d_from, args.d_to, coll_name=_coll_for(args.instrument))
        return 0

    d = date.fromisoformat(args.d_from)
    end = date.fromisoformat(args.d_to)
    total_days = total_snaps = 0
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            t0 = time.time()
            try:
                n = backfill_day(db, args.instrument, d.isoformat(), args.strikes)
            except Exception as exc:
                print(f"{d} ERROR {str(exc)[:140]}", flush=True)
                n = -1
            if n > 0:
                total_days += 1
                total_snaps += n
                print(f"{d} OK {n} snapshots ({time.time()-t0:.0f}s) "
                      f"[{total_days} days, {total_snaps} total]", flush=True)
            elif n == 0:
                print(f"{d} holiday/empty", flush=True)
            time.sleep(args.pause)
        d += timedelta(days=1)
    print(f"BACKFILL_DONE days={total_days} snapshots={total_snaps}", flush=True)
    if not args.skip_second_passes and total_days > 0:
        # iv_derived.iv_percentile/iv_regime and session_levels both need
        # chronological state across multiple already-inserted days -- were
        # previously opt-in only (--iv-pass) and not run by default, which is
        # how the schema gap survived undetected. Now run automatically so a
        # normal backfill run produces full-schema data without a manual
        # second step to remember.
        iv_percentile_pass(db, args.d_from, args.d_to, coll_name=_coll_for(args.instrument))
        session_levels_pass(db, args.d_from, args.d_to, coll_name=_coll_for(args.instrument))
    return 0


if __name__ == "__main__":
    sys.exit(main())
