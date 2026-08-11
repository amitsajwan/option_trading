"""Convert fetched Dhan historical data into runtime snapshot_raw_json format.

Reuses the logic from ml_pipeline_2/scripts/dhan_build_sim_snapshots.py but
operates on the in-memory dict returned by DhanHistoricalFetcher.fetch_day()
instead of reading from raw/ parquet files.

Output: list of snapshot dicts with snapshot_raw_json (same format as
snapshot_app publishes live) — ready to be published via replay_runner.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from snapshot_app.core.market_snapshot import _ladder_aggregates, _normalize_iv
from snapshot_app.core.market_snapshot_contract import REQUIRED_BLOCK_FIELDS

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Skeleton key sets for the blocks filled in by later enrichment passes
# (enrich_day_mtf_opening_iv, session_levels_pass) -- sourced from the same
# contract the validator checks against, so this can't silently drift from
# it the way the original builder did.
_MTF_KEYS = REQUIRED_BLOCK_FIELDS["mtf_derived"]
_OPENING_RANGE_KEYS = REQUIRED_BLOCK_FIELDS["opening_range"]
_IV_DERIVED_KEYS = REQUIRED_BLOCK_FIELDS["iv_derived"]
_SESSION_LEVELS_KEYS = REQUIRED_BLOCK_FIELDS["session_levels"]

# ── helpers ────────────────────────────────────────────────────────────────────

def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _ts_ist(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST)
        return dt.astimezone(_IST)
    except Exception:
        return None


# ── main builder ───────────────────────────────────────────────────────────────

def build_snapshots_from_dhan_data(
    raw: Dict[str, Any],
    progress_cb=None,
) -> List[Dict[str, Any]]:
    """Convert raw Dhan data dict into a list of snapshot dicts.

    Each snapshot has a 'snapshot_raw_json' key so replay_runner can publish
    it directly to Redis, identical to what snapshot_app produces live.
    """
    def _progress(msg: str):
        if progress_cb:
            progress_cb("build", msg)
        logger.info("build: %s", msg)

    instrument = raw.get("instrument", "NIFTY")
    trade_date = raw.get("trade_date", "")
    step = raw.get("step", 50)
    atm_strike = raw.get("atm_strike")
    index_bars = raw.get("index_bars", [])
    vix_bars = raw.get("vix_bars", [])
    options = raw.get("options", {})

    _progress(f"Building snapshots for {instrument} {trade_date} ({len(index_bars)} index bars)")

    # Build VIX lookup: ts → vix_close
    vix_by_ts: Dict[str, float] = {}
    for b in vix_bars:
        ts = b.get("ts", "")[:16]
        if ts and b.get("close"):
            vix_by_ts[ts] = float(b["close"])

    # Build options lookup: offset_label → {ts → {ce/pe fields}}
    # First collect all per-minute timestamps
    option_by_ts: Dict[str, Dict[str, Dict[str, Optional[float]]]] = {}
    for label, sides in options.items():
        for side, bars in sides.items():
            for b in bars:
                ts = b.get("ts", "")[:16]
                if not ts:
                    continue
                if ts not in option_by_ts:
                    option_by_ts[ts] = {}
                if label not in option_by_ts[ts]:
                    option_by_ts[ts][label] = {}
                for k, v in b.items():
                    if k == "ts":
                        continue
                    if k.endswith("_iv"):
                        # Found 2026-08-11: Dhan's rollingoption IV comes back
                        # as raw percentage points (e.g. 11.47 meaning 11.47%),
                        # ~100x live's decimal convention (0.1147) -- live's
                        # _compute_iv always runs this same normalization.
                        # Left un-normalized, every IV-derived field here
                        # (atm_ce_iv/pe_iv, iv_skew, iv_percentile via
                        # iv_percentile_pass) was internally self-consistent
                        # but on a different scale than live, undermining
                        # any comparison between backfilled and live IV-rank.
                        option_by_ts[ts][label][k] = _normalize_iv(_f(v) or float("nan"))
                    else:
                        option_by_ts[ts][label][k] = _f(v)

    # Running state for accumulators (day_high, day_low, open etc)
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    day_open: Optional[float] = None
    bars_seen = 0
    snapshots = []
    prev_atm_ce_ltp: Optional[float] = None
    prev_atm_pe_ltp: Optional[float] = None
    prev_atm_ce_oi: Optional[float] = None
    prev_atm_pe_oi: Optional[float] = None

    for i, bar in enumerate(index_bars):
        ts_str = bar.get("ts") or bar.get("start_at") or ""
        ts = _ts_ist(ts_str)
        if ts is None:
            continue
        ts_key = ts_str[:16]

        close = _f(bar.get("close"))
        open_ = _f(bar.get("open"))
        high = _f(bar.get("high"))
        low = _f(bar.get("low"))
        volume = _f(bar.get("volume"))

        if close is None:
            continue

        # Update day accumulators
        if day_open is None:
            day_open = open_
        if day_high is None or (high and high > day_high):
            day_high = high
        if day_low is None or (low and low < day_low):
            day_low = low
        bars_seen += 1

        # VIX for this bar
        vix_close = vix_by_ts.get(ts_key)
        vix_prev = None
        if vix_close and i > 0:
            prev_ts = (index_bars[i-1].get("ts",""))[:16]
            vix_prev = vix_by_ts.get(prev_ts)
        vix_chg = (vix_close - vix_prev) / vix_prev if (vix_close and vix_prev and vix_prev > 0) else None

        # Options for this bar
        opt_ts = option_by_ts.get(ts_key, {})

        # ATM option data
        atm_data = opt_ts.get("ATM", {})
        atm_ce_ltp = atm_data.get("ce_close")
        atm_pe_ltp = atm_data.get("pe_close")
        atm_ce_open = atm_data.get("ce_open")
        atm_ce_high = atm_data.get("ce_high")
        atm_ce_low = atm_data.get("ce_low")
        atm_pe_open = atm_data.get("pe_open")
        atm_pe_high = atm_data.get("pe_high")
        atm_pe_low = atm_data.get("pe_low")
        atm_ce_iv = atm_data.get("ce_iv")
        atm_pe_iv = atm_data.get("pe_iv")
        atm_ce_oi = atm_data.get("ce_oi")
        atm_pe_oi = atm_data.get("pe_oi")
        atm_strike_this_bar = atm_data.get("strike")

        # Build strikes list for chain. 2026-07-10: rollingoption rungs are
        # RELATIVE to spot and roll intrabar — each bar carries its true
        # absolute strike ('strike' in the bar). Key the chain by THAT, merging
        # CE/PE series that land on the same absolute strike this minute. The
        # old label-derived strike (open-anchored ATM + offset*step) is only a
        # fallback for data recorded before the fix.
        by_strike: Dict[int, Dict[str, Optional[float]]] = {}
        total_ce_oi = 0.0
        total_pe_oi = 0.0
        for label, side_data in opt_ts.items():
            if not label.startswith("ATM"):
                continue
            if label == "ATM":
                off = 0
            elif label.startswith("ATMp"):
                off = int(label[4:])
            elif label.startswith("ATMm"):
                off = -int(label[4:])
            else:
                continue
            fallback_sk = (atm_strike or close) + off * step if atm_strike or close else None
            sk = side_data.get("strike") or fallback_sk
            if sk is None:
                continue
            row = by_strike.setdefault(int(sk), {})
            for f in ("ce_close", "ce_open", "ce_high", "ce_low", "ce_oi", "ce_iv",
                      "pe_close", "pe_open", "pe_high", "pe_low", "pe_oi", "pe_iv"):
                if side_data.get(f) is not None:
                    row[f] = side_data[f]
        strikes = []
        for sk in sorted(by_strike):
            side_data = by_strike[sk]
            ce_oi = side_data.get("ce_oi") or 0.0
            pe_oi = side_data.get("pe_oi") or 0.0
            total_ce_oi += float(ce_oi) if ce_oi else 0.0
            total_pe_oi += float(pe_oi) if pe_oi else 0.0
            strikes.append({
                "strike": sk,
                "ce_ltp": side_data.get("ce_close"),
                "ce_open": side_data.get("ce_open"),
                "ce_high": side_data.get("ce_high"),
                "ce_low": side_data.get("ce_low"),
                "ce_oi": ce_oi,
                "ce_iv": side_data.get("ce_iv"),
                # Per-strike option volume: same unavailable-from-Dhan gap as
                # the ATM/total volume fields above -- key present, value None.
                "ce_volume": None,
                "pe_ltp": side_data.get("pe_close"),
                "pe_open": side_data.get("pe_open"),
                "pe_high": side_data.get("pe_high"),
                "pe_low": side_data.get("pe_low"),
                "pe_oi": pe_oi,
                "pe_iv": side_data.get("pe_iv"),
                "pe_volume": None,
            })

        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else None
        atm_iv = ((atm_ce_iv or 0) + (atm_pe_iv or 0)) / 2 if (atm_ce_iv and atm_pe_iv) else None
        atm_strike_final = round(close / step) * step if close else None

        # ce_oi_top_strike/pe_oi_top_strike: strike with the largest OI on each side.
        ce_oi_top_strike = None
        pe_oi_top_strike = None
        if strikes:
            ce_ranked = [s for s in strikes if s.get("ce_oi")]
            pe_ranked = [s for s in strikes if s.get("pe_oi")]
            if ce_ranked:
                ce_oi_top_strike = max(ce_ranked, key=lambda s: s["ce_oi"])["strike"]
            if pe_ranked:
                pe_oi_top_strike = max(pe_ranked, key=lambda s: s["pe_oi"])["strike"]

        atm_straddle_price = (atm_ce_ltp + atm_pe_ltp) if (atm_ce_ltp is not None and atm_pe_ltp is not None) else None
        atm_straddle_pct = (atm_straddle_price / close) if (atm_straddle_price is not None and close) else None

        atm_ce_return_1m = (
            (atm_ce_ltp - prev_atm_ce_ltp) / prev_atm_ce_ltp
            if (atm_ce_ltp is not None and prev_atm_ce_ltp) else None
        )
        atm_pe_return_1m = (
            (atm_pe_ltp - prev_atm_pe_ltp) / prev_atm_pe_ltp
            if (atm_pe_ltp is not None and prev_atm_pe_ltp) else None
        )
        atm_ce_oi_change_1m = (atm_ce_oi - prev_atm_ce_oi) if (atm_ce_oi is not None and prev_atm_ce_oi is not None) else None
        atm_pe_oi_change_1m = (atm_pe_oi - prev_atm_pe_oi) if (atm_pe_oi is not None and prev_atm_pe_oi is not None) else None

        # Build the snapshot dict in runtime format
        snapshot = {
            # Was "SnapshotMLFlat" -- didn't match market_snapshot_contract's
            # required "MarketSnapshot" (found 2026-08-11), failing every bar.
            "schema_name": "MarketSnapshot",
            "schema_version": "3.0",
            "build_source": "dhan_replay",
            "build_run_id": f"replay-{trade_date}-{instrument}",
            "snapshot_id": f"{trade_date.replace('-','')}_{ts.strftime('%H%M')}",
            "instrument": f"{instrument}FUT",
            "trade_date": trade_date,
            "timestamp": ts.isoformat(),
            "session_context": {
                "snapshot_id": f"{trade_date.replace('-','')}_{ts.strftime('%H%M')}",
                "timestamp": ts.isoformat(),
                "date": trade_date,
                "time": ts.strftime("%H:%M:%S"),
                "minutes_since_open": max(0, (ts.hour * 60 + ts.minute) - (9 * 60 + 15)),
                "minutes_to_close": max(0, (15 * 60 + 30) - (ts.hour * 60 + ts.minute)),
                "day_of_week": ts.weekday(),
                "days_to_expiry": None,
                "is_expiry_day": None,
                "session_phase": _session_phase(ts),
                "is_first_hour": ts.hour == 9,
                "is_last_hour": ts.hour >= 14,
            },
            "futures_bar": {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                # fut_* aliases -- SnapshotAccessor/market_snapshot_contract read
                # these names, not the bare open/high/low/close/volume above
                # (kept for backward compat with anything already reading them).
                "fut_open": open_,
                "fut_high": high,
                "fut_low": low,
                "fut_close": close,
                "fut_volume": volume,
                # Futures OI: not fetched by get_historical_day's index/futures
                # bars today (index proxy has no OI concept) -- real gap, not
                # a wiring miss like the option OHLC one was. Leave None.
                "fut_oi": None,
            },
            "futures_derived": {
                "fut_return_1m": _pct_chg(index_bars, i, 1),
                "fut_return_3m": _pct_chg(index_bars, i, 3),
                "fut_return_5m": _pct_chg(index_bars, i, 5),
                "fut_return_15m": _pct_chg(index_bars, i, 15),
                "fut_return_30m": _pct_chg(index_bars, i, 30),
                "dist_from_day_high": (close - day_high) / day_high if (close and day_high and day_high > 0) else None,
                "dist_from_day_low": (close - day_low) / day_low if (close and day_low and day_low > 0) else None,
                "atr_ratio": _atr_ratio(index_bars, i, 14),
                # atr_daily_percentile needs a multi-day ATR history (rolling
                # percentile across sessions) -- not available at single-day
                # build time, same shape as the cross-day passes below. None
                # for now; a future atr_daily_percentile_pass could add it.
                "atr_daily_percentile": None,
                "vol_ratio": _vol_ratio(index_bars, i, 30),
                # Same ratio as vol_ratio -- contract lists both names
                # (futures_derived.vol_ratio vs fut_volume_ratio); live keeps
                # them as one value under two keys, mirrored here.
                "fut_volume_ratio": _vol_ratio(index_bars, i, 30),
                "realized_vol_30m": _realized_vol_30m(index_bars, i, 30),
                # Needs futures OI, which is unavailable (see futures_bar.fut_oi
                # above) -- real gap, not fixable at this layer.
                "fut_oi_change_30m": None,
                # ema_9/21/50(+slopes)/vwap/price_vs_vwap filled by
                # enrich_day_ema_and_aggregates / enrich_day_mtf_opening_iv
                # after the full day is built (need day-level history).
                "ema_9": None,
                "ema_21": None,
                "ema_50": None,
                "ema_9_slope": None,
                "ema_21_slope": None,
                "ema_50_slope": None,
                "vwap": None,
                "price_vs_vwap": None,
            },
            "atm_options": {
                "atm_ce_strike": atm_strike_this_bar or atm_strike_final,
                "atm_pe_strike": atm_strike_this_bar or atm_strike_final,
                "atm_ce_open": atm_ce_open,
                "atm_ce_high": atm_ce_high,
                "atm_ce_low": atm_ce_low,
                "atm_ce_close": atm_ce_ltp,
                "atm_ce_return_1m": atm_ce_return_1m,
                # Dhan's rollingoption endpoint does not carry per-strike option
                # VOLUME in practice (requested via requiredData, comes back
                # empty) -- documented upstream limit, see chain_aggregates
                # below. atm_ce_vol_ratio is therefore permanently unavailable.
                "atm_ce_volume": None,
                "atm_ce_oi": atm_ce_oi,
                "atm_ce_oi_change_1m": atm_ce_oi_change_1m,
                "atm_ce_oi_change_30m": None,  # filled by enrich_day_ema_and_aggregates
                "atm_ce_iv": atm_ce_iv,
                "atm_ce_vol_ratio": None,
                "atm_pe_strike": atm_strike_this_bar or atm_strike_final,
                "atm_pe_open": atm_pe_open,
                "atm_pe_high": atm_pe_high,
                "atm_pe_low": atm_pe_low,
                "atm_pe_close": atm_pe_ltp,
                "atm_pe_return_1m": atm_pe_return_1m,
                "atm_pe_volume": None,
                "atm_pe_oi": atm_pe_oi,
                "atm_pe_oi_change_1m": atm_pe_oi_change_1m,
                "atm_pe_oi_change_30m": None,  # filled by enrich_day_ema_and_aggregates
                "atm_pe_iv": atm_pe_iv,
                "atm_pe_vol_ratio": None,
                "atm_ce_pe_price_diff": (
                    (atm_ce_ltp - atm_pe_ltp) if (atm_ce_ltp is not None and atm_pe_ltp is not None) else None
                ),
                "atm_ce_pe_iv_diff": (atm_ce_iv - atm_pe_iv) if (atm_ce_iv and atm_pe_iv) else None,
                "atm_oi_ratio": (atm_ce_oi / atm_pe_oi) if (atm_ce_oi and atm_pe_oi and atm_pe_oi > 0) else None,
                # extra, non-contract fields kept for existing consumers:
                "atm_iv": atm_iv,
                "iv_skew": (atm_ce_iv - atm_pe_iv) if (atm_ce_iv and atm_pe_iv) else None,
            },
            "chain_aggregates": {
                "pcr": pcr,
                # pcr_change_5m/15m/30m need multi-bar PCR history -- not
                # available at this per-bar construction point (mirrors the
                # existing total_ce_volume/pe_volume gap below). Left None;
                # key presence is all the schema contract requires.
                "pcr_change_5m": None,
                "pcr_change_15m": None,
                "pcr_change_30m": None,
                "atm_oi_ratio": (atm_ce_oi / atm_pe_oi) if (atm_ce_oi and atm_pe_oi and atm_pe_oi > 0) else None,
                "near_atm_oi_ratio": None,
                "strike_count": len(strikes),
                "ce_oi_top_strike": ce_oi_top_strike,
                "pe_oi_top_strike": pe_oi_top_strike,
                "ce_pe_oi_diff": (total_ce_oi - total_pe_oi) if (total_ce_oi or total_pe_oi) else None,
                # Filled by enrich_day_ema_and_aggregates once >=10 strikes are
                # priced for this bar; placeholder so the key is always present.
                "max_pain": None,
                # Already summed above for pcr (2026-07-31 fix) -- omitting these
                # left every OI-based velocity feature (vel_ce_oi_delta_*, etc.)
                # permanently NaN, since live_velocity_state._extract_morning_row
                # reads them from here. Dhan's rollingoption endpoint doesn't
                # carry per-strike option VOLUME (only premium/IV/OI), so
                # total_ce_volume/total_pe_volume stay unavailable here --
                # that's a real upstream data-source limit, not fixable at this
                # layer; it also means the velocity context provider's prior-day
                # midday-volume lookup (which needs volume, not OI) will keep
                # returning None here, leaving ctx_gap_*/vol_spike_ratio NaN.
                "total_ce_oi": total_ce_oi,
                "total_pe_oi": total_pe_oi,
                "total_ce_volume": None,
                "total_pe_volume": None,
                "ce_pe_volume_diff": None,
                "atm_straddle_price": atm_straddle_price,
                "atm_straddle_pct": atm_straddle_pct,
                # distance_to_max_pain_pct needs max_pain, which is only known
                # after the full day's OI history is seen -- filled by
                # enrich_day_ema_and_aggregates once max_pain lands.
                "distance_to_max_pain_pct": None,
                # Required by chain_utils' far-OTM price proxy — without it, a
                # strike that drifts off the chain can never be valued (froze a
                # spread for 11 months in replay; 2026-07-10).
                "atm_strike": atm_strike_final,
            },
            "vix_context": {
                "vix_current": vix_close,
                "vix_prev_close": vix_prev,
                "vix_intraday_chg": vix_chg,
                "vix_regime": None,
                "vix_spike_flag": False,
            },
            "strikes": strikes,
            # ladder_aggregates is genuinely per-bar (only needs this bar's
            # strikes + totals), so compute it directly with the live
            # builder's own function -- one source of truth, no re-drift.
            # total_ce_volume/total_pe_volume are unavailable (see
            # chain_aggregates comment above), so the volume-based sub-fields
            # correctly come back None; that's real, not a bug here.
            "ladder_aggregates": _ladder_aggregates(
                strikes,
                atm_strike=atm_strike_final,
                total_ce_oi=total_ce_oi,
                total_pe_oi=total_pe_oi,
                total_ce_volume=0.0,
                total_pe_volume=0.0,
            ),
            # mtf_derived/opening_range need growing multi-bar history (5m/15m
            # resampling, the day's opening 15 minutes) -- filled by
            # enrich_day_mtf_opening_iv() after this whole day is built, same
            # pattern as enrich_day_ema_and_aggregates. Skeleton here only
            # so the schema contract's key-presence check is satisfied even
            # before that pass runs.
            "mtf_derived": dict.fromkeys(_MTF_KEYS),
            "opening_range": dict.fromkeys(_OPENING_RANGE_KEYS),
            # iv_skew/iv_skew_dir/iv_expiry_type need is_expiry_day, which
            # _enrich_for_seller() sets AFTER this function returns; iv_percentile/
            # iv_regime need the live cross-day rolling history (iv_percentile_pass).
            # All filled downstream -- skeleton here for the same reason as above.
            "iv_derived": dict.fromkeys(_IV_DERIVED_KEYS),
            # session_levels needs the prior trading day(s)' data -- filled by
            # session_levels_pass() once every day in the range is in Mongo.
            "session_levels": dict.fromkeys(_SESSION_LEVELS_KEYS),
        }
        snapshots.append(snapshot)
        prev_atm_ce_ltp = atm_ce_ltp if atm_ce_ltp is not None else prev_atm_ce_ltp
        prev_atm_pe_ltp = atm_pe_ltp if atm_pe_ltp is not None else prev_atm_pe_ltp
        prev_atm_ce_oi = atm_ce_oi if atm_ce_oi is not None else prev_atm_ce_oi
        prev_atm_pe_oi = atm_pe_oi if atm_pe_oi is not None else prev_atm_pe_oi

    _progress(f"Built {len(snapshots)} snapshots")
    return snapshots


def _session_phase(ts: datetime) -> str:
    m = ts.hour * 60 + ts.minute
    if m < 9 * 60 + 45:
        return "PRE_OPEN"
    if m < 11 * 60 + 30:
        return "MORNING"
    if m < 14 * 60 + 30:
        return "ACTIVE"
    if m < 15 * 60 + 15:
        return "PRE_CLOSE"
    return "CLOSED"


def _pct_chg(bars: List[dict], i: int, n: int) -> Optional[float]:
    if i < n:
        return None
    curr = _f(bars[i].get("close"))
    prev = _f(bars[i - n].get("close"))
    if curr and prev and prev > 0:
        return (curr - prev) / prev
    return None


def _vol_ratio(bars: List[dict], i: int, period: int = 30) -> Optional[float]:
    """Current bar volume / average volume over past `period` bars."""
    curr_vol = _f(bars[i].get("volume"))
    if curr_vol is None:
        return None
    start = max(0, i - period)
    vols = [_f(bars[j].get("volume")) for j in range(start, i)]
    vols = [v for v in vols if v is not None and v > 0]
    if not vols:
        return None
    avg = sum(vols) / len(vols)
    return curr_vol / avg if avg > 0 else None


def _realized_vol_30m(bars: List[dict], i: int, period: int = 30) -> Optional[float]:
    """Std dev of last `period` 1-min log returns — proxy for 30-min realized vol."""
    import math
    start = max(1, i - period + 1)
    rets = []
    for j in range(start, i + 1):
        c = _f(bars[j].get("close"))
        p = _f(bars[j - 1].get("close"))
        if c and p and p > 0:
            rets.append(math.log(c / p))
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(variance)


def _atr_ratio(bars: List[dict], i: int, period: int = 14) -> Optional[float]:
    start = max(0, i - period)
    window = bars[start:i + 1]
    if len(window) < 3:
        return None
    trs = []
    for j in range(1, len(window)):
        h = _f(window[j].get("high"))
        l = _f(window[j].get("low"))
        pc = _f(window[j-1].get("close"))
        if h and l and pc:
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    atr = sum(trs) / len(trs)
    close = _f(bars[i].get("close"))
    return atr / close if (close and close > 0) else None
