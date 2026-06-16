#!/usr/bin/env python3
"""D1-S1: Direction-prediction audit framework.

Measures predictive power of candidate features from `phase1_market_snapshots`
for futures direction at 1m / 5m / 15m horizons.

Usage:
    # Basic — audit all pre-registered features, last 30 days
    python docs/audits/direction_audit_template.py

    # Specific date range
    python docs/audits/direction_audit_template.py --start 2026-05-26 --end 2026-05-30

    # Custom mongo connection
    python docs/audits/direction_audit_template.py --mongo mongodb://localhost:27017 --db trading_ai

    # Save results to markdown
    python docs/audits/direction_audit_template.py --save docs/audits/CHAIN_FEATURES_DIRECTION_AUDIT_2026-05-26.md

Exit codes:
    0  — at least one feature passed all gates
    1  — no features passed (or insufficient data)

==============================================================================
PRE-REGISTERED VERDICT GATES (FROZEN — do not change after first run)
==============================================================================
    CI_LB > 0.50   (bootstrap 95% CI lower bound on hit-rate)
    POS_DAYS_PCT >= 0.60  (fraction of trading days with positive hit-rate)
    N_OBS >= 200          (minimum observations across all dates)

These constants are written into the code, not parameters. Any change requires
a new ticket on the scrum board (D1-S1 gate amendment). Do NOT tune to make a
feature pass.
==============================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# ── Pre-registered gates (FROZEN) ──────────────────────────────────────────
GATE_CI_LB = 0.50          # bootstrap 95% CI lower bound must exceed this
GATE_POS_DAYS_PCT = 0.60   # fraction of days with positive hit-rate
GATE_MIN_OBS = 200         # minimum total observations
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 42
HORIZONS_MIN = [1, 5, 15]  # prediction horizons in minutes
# ───────────────────────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))


def _f(v: Any) -> Optional[float]:
    try:
        r = float(v)
        return r if r == r else None
    except (TypeError, ValueError):
        return None


def _bootstrap_ci_lb(hits: list[int], n_resamples: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED) -> float:
    """Return lower bound of 95% bootstrap CI on hit-rate."""
    rng = random.Random(seed)
    n = len(hits)
    if n == 0:
        return 0.0
    means = []
    for _ in range(n_resamples):
        sample = [hits[rng.randint(0, n - 1)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    idx_lb = int(0.025 * n_resamples)
    return float(means[idx_lb])


def _sign(v: Optional[float]) -> Optional[int]:
    if v is None:
        return None
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


# ── Feature extractor registry ─────────────────────────────────────────────
# Each extractor: fn(snapshot_dict) -> Optional[float]
# Positive value = predicts CE (up); Negative = predicts PE (down)

def _snap(doc: dict) -> dict:
    """Navigate payload.snapshot from a Mongo doc."""
    p = doc.get("payload") or {}
    if isinstance(p, dict):
        s = p.get("snapshot") or {}
        if isinstance(s, dict):
            return s
    return {}


def _ca(doc: dict) -> dict:
    return _snap(doc).get("chain_aggregates") or {}


def _atm(doc: dict) -> dict:
    return _snap(doc).get("atm_options") or {}


def _vix(doc: dict) -> dict:
    return _snap(doc).get("vix_context") or {}


def _fb(doc: dict) -> dict:
    return _snap(doc).get("futures_bar") or {}


def _orng(doc: dict) -> dict:
    return _snap(doc).get("opening_range") or {}


FEATURE_EXTRACTORS: dict[str, Any] = {
    # ── Chain aggregates ───────────────────────────────────────────────────
    "pcr": lambda d: (
        lambda v: (-1.0 * v) if v is not None else None  # high PCR = more puts = bearish
    )(_f(_ca(d).get("pcr"))),

    "pcr_change_5m": lambda d: (
        lambda v: (-1.0 * v) if v is not None else None
    )(_f(_ca(d).get("pcr_change_5m"))),

    "pcr_change_15m": lambda d: (
        lambda v: (-1.0 * v) if v is not None else None
    )(_f(_ca(d).get("pcr_change_15m"))),

    "ce_pe_oi_diff": lambda d: _f(_ca(d).get("ce_pe_oi_diff")),        # CE-PE OI diff > 0 = more CE OI = bearish
    "ce_pe_volume_diff": lambda d: _f(_ca(d).get("ce_pe_volume_diff")),

    # ── ATM options ────────────────────────────────────────────────────────
    "atm_oi_ratio": lambda d: _f(_atm(d).get("atm_oi_ratio")),         # CE/PE OI at ATM

    "atm_oi_imbalance": lambda d: (
        lambda ce, pe: (ce - pe) if ce is not None and pe is not None else None
    )(
        _f(_atm(d).get("atm_ce_oi_change_1m")),
        _f(_atm(d).get("atm_pe_oi_change_1m")),
    ),

    "iv_skew": lambda d: (
        lambda ce_iv, pe_iv: (pe_iv - ce_iv) if ce_iv is not None and pe_iv is not None else None
        # PE IV > CE IV = put skew = bearish
    )(
        _f(_atm(d).get("atm_ce_iv")),
        _f(_atm(d).get("atm_pe_iv")),
    ),

    # ── Distance to max pain ──────────────────────────────────────────────
    "dist_to_max_pain_pct": lambda d: (
        lambda v: (-1.0 * v) if v is not None else None
        # Negative dist = price below max pain = pull up = CE
        # Positive dist = price above max pain = pull down = PE
    )(_f(_ca(d).get("distance_to_max_pain_pct"))),

    # ── Trap signals (from E5-S1, folded into D1-S2 per sprint 4 board) ──
    # These are computed by the shadow scorer; audit whether they appear as
    # explicit fields in the snapshot or must be derived here.
    # For now: derive them from the raw fields in atm_options / futures_bar.

    "orb_low_rejected": lambda d: _derive_orb_low_rejected(d),
    "orb_high_rejected": lambda d: _derive_orb_high_rejected(d),
    "vwap_reclaim_bull": lambda d: _derive_vwap_reclaim_bull(d),
    "vwap_reject_bear": lambda d: _derive_vwap_reject_bear(d),
}


def _derive_orb_low_rejected(d: dict) -> Optional[float]:
    """Price broke ORB low then reclaimed — bullish CE signal (+1) or neutral (0)."""
    orng = _orng(d)
    fb = _fb(d)
    orb_low = _f(orng.get("orb_low"))
    prev_low = _f(fb.get("prev_low") or fb.get("low"))
    cur_close = _f(fb.get("fut_close") or fb.get("close"))
    if orb_low is None or prev_low is None or cur_close is None:
        return None
    if prev_low < orb_low and cur_close > orb_low:
        return 1.0
    return 0.0


def _derive_orb_high_rejected(d: dict) -> Optional[float]:
    """Price broke ORB high then failed — bearish PE signal (-1) or neutral (0)."""
    orng = _orng(d)
    fb = _fb(d)
    orb_high = _f(orng.get("orb_high"))
    prev_high = _f(fb.get("prev_high") or fb.get("high"))
    cur_close = _f(fb.get("fut_close") or fb.get("close"))
    if orb_high is None or prev_high is None or cur_close is None:
        return None
    if prev_high > orb_high and cur_close < orb_high:
        return -1.0
    return 0.0


def _derive_vwap_reclaim_bull(d: dict) -> Optional[float]:
    """Price was below VWAP, now above — bullish (+1) or neutral (0)."""
    fb = _fb(d)
    fd = (_snap(d).get("futures_derived") or {})
    vwap = _f(fd.get("vwap_session") or fd.get("vwap"))
    prev_close = _f(fb.get("prev_close"))
    cur_close = _f(fb.get("fut_close") or fb.get("close"))
    if vwap is None or prev_close is None or cur_close is None:
        return None
    if prev_close < vwap and cur_close > vwap:
        return 1.0
    return 0.0


def _derive_vwap_reject_bear(d: dict) -> Optional[float]:
    """Price was above VWAP, now below — bearish (-1) or neutral (0)."""
    fb = _fb(d)
    fd = (_snap(d).get("futures_derived") or {})
    vwap = _f(fd.get("vwap_session") or fd.get("vwap"))
    prev_close = _f(fb.get("prev_close"))
    cur_close = _f(fb.get("fut_close") or fb.get("close"))
    if vwap is None or prev_close is None or cur_close is None:
        return None
    if prev_close > vwap and cur_close < vwap:
        return -1.0
    return 0.0


# ── Data loading ────────────────────────────────────────────────────────────

def _load_snapshots(
    *,
    mongo_uri: str,
    db_name: str,
    collection: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    try:
        from pymongo import MongoClient  # type: ignore[import-untyped]
    except ImportError:
        print("[ERROR] pymongo not installed. Run: pip install pymongo", file=sys.stderr)
        sys.exit(2)

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
    db = client[db_name]
    coll = db[collection]
    cursor = coll.find(
        {"trade_date_ist": {"$gte": start_date, "$lte": end_date}},
        {"payload": 1, "trade_date_ist": 1, "timestamp": 1, "_id": 0},
        sort=[("trade_date_ist", 1), ("timestamp", 1)],
    )
    docs = list(cursor)
    client.close()
    return docs


def _build_timeline(docs: list[dict]) -> dict[str, list[dict]]:
    """Group docs by trade_date_ist."""
    by_date: dict[str, list[dict]] = defaultdict(list)
    for doc in docs:
        d = str(doc.get("trade_date_ist") or "").strip()
        if d:
            by_date[d].append(doc)
    return dict(by_date)


def _get_fut_close_at_offset(timeline_for_date: list[dict], idx: int, offset_min: int) -> Optional[float]:
    """Return fut_close `offset_min` bars ahead of `idx` in the same day."""
    target_idx = idx + offset_min
    if target_idx >= len(timeline_for_date):
        return None
    return _f(_fb(timeline_for_date[target_idx]).get("fut_close") or _fb(timeline_for_date[target_idx]).get("close"))


# ── Audit engine ────────────────────────────────────────────────────────────

def audit_features(
    docs: list[dict],
    feature_names: list[str],
) -> dict[str, Any]:
    """Run hit-rate audit for each feature at each horizon.

    Returns a nested dict: feature -> horizon -> {hit_rate, ci_lb, pos_days_pct, n_obs, verdict}
    """
    by_date = _build_timeline(docs)
    results: dict[str, dict[int, dict]] = {f: {} for f in feature_names}

    for horizon in HORIZONS_MIN:
        feature_obs: dict[str, list[int]] = {f: [] for f in feature_names}
        feature_obs_by_day: dict[str, dict[str, list[int]]] = {f: defaultdict(list) for f in feature_names}

        for trade_date, day_docs in sorted(by_date.items()):
            for idx, doc in enumerate(day_docs):
                cur_close = _f(_fb(doc).get("fut_close") or _fb(doc).get("close"))
                fwd_close = _get_fut_close_at_offset(day_docs, idx, horizon)
                if cur_close is None or fwd_close is None:
                    continue
                actual_dir = _sign(fwd_close - cur_close)
                if actual_dir == 0:
                    continue

                for fname in feature_names:
                    extractor = FEATURE_EXTRACTORS.get(fname)
                    if extractor is None:
                        continue
                    try:
                        feat_val = extractor(doc)
                    except Exception:
                        feat_val = None
                    if feat_val is None:
                        continue
                    pred_dir = _sign(feat_val)
                    if pred_dir == 0:
                        continue
                    hit = 1 if pred_dir == actual_dir else 0
                    feature_obs[fname].append(hit)
                    feature_obs_by_day[fname][trade_date].append(hit)

        for fname in feature_names:
            obs = feature_obs[fname]
            n = len(obs)
            if n == 0:
                results[fname][horizon] = {
                    "hit_rate": None, "ci_lb": None,
                    "pos_days_pct": None, "n_obs": 0, "verdict": "NO_DATA",
                }
                continue

            hit_rate = sum(obs) / n
            ci_lb = _bootstrap_ci_lb(obs)
            day_rates = {
                d: sum(v) / len(v)
                for d, v in feature_obs_by_day[fname].items()
                if v
            }
            pos_days_pct = sum(1 for r in day_rates.values() if r > 0.5) / len(day_rates) if day_rates else 0.0

            verdict = "PASS" if (
                ci_lb > GATE_CI_LB
                and pos_days_pct >= GATE_POS_DAYS_PCT
                and n >= GATE_MIN_OBS
            ) else "FAIL"

            results[fname][horizon] = {
                "hit_rate": round(hit_rate, 4),
                "ci_lb": round(ci_lb, 4),
                "pos_days_pct": round(pos_days_pct, 4),
                "n_obs": n,
                "verdict": verdict,
            }

    return results


# ── Reporting ───────────────────────────────────────────────────────────────

def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "    N/A"
    return f"{v * 100:6.1f}%"


def _print_results(results: dict, start_date: str, end_date: str, n_docs: int) -> None:
    print(f"\n{'=' * 72}")
    print(f"Direction Feature Audit — {start_date} → {end_date}  ({n_docs} snapshots)")
    print(f"Gates (FROZEN): CI_lb > {GATE_CI_LB}, pos_days ≥ {GATE_POS_DAYS_PCT*100:.0f}%, n ≥ {GATE_MIN_OBS}")
    print(f"{'=' * 72}")

    for horizon in HORIZONS_MIN:
        print(f"\n  Horizon: +{horizon}m")
        print(f"  {'Feature':<28} {'HitRate':>8} {'CI_lb':>7} {'PosDays':>8} {'N_obs':>6}  Verdict")
        print(f"  {'-' * 68}")
        rows = []
        for fname, h_results in results.items():
            r = h_results.get(horizon, {})
            rows.append((fname, r))
        rows.sort(key=lambda x: x[1].get("ci_lb") or 0.0, reverse=True)
        for fname, r in rows:
            v = r.get("verdict", "NO_DATA")
            marker = "  *** PASS ***" if v == "PASS" else ""
            print(
                f"  {fname:<28} "
                f"{_fmt_pct(r.get('hit_rate'))} "
                f"{_fmt_pct(r.get('ci_lb'))} "
                f"{_fmt_pct(r.get('pos_days_pct'))} "
                f"{r.get('n_obs', 0):>6}  {v}{marker}"
            )

    passes = [
        fname
        for fname, h_results in results.items()
        if any(r.get("verdict") == "PASS" for r in h_results.values())
    ]
    print(f"\n{'=' * 72}")
    if passes:
        print(f"FEATURES THAT PASSED (≥1 horizon): {', '.join(passes)}")
    else:
        print("NO features passed all gates at any horizon.")
        if n_docs < GATE_MIN_OBS:
            print(f"NOTE: Only {n_docs} snapshots available — need ≥{GATE_MIN_OBS}.")
            print("      Today was likely expiry/abnormal. Repeat audit after 3+ normal sessions.")

    print()


def _results_to_markdown(results: dict, start_date: str, end_date: str, n_docs: int) -> str:
    lines = [
        f"# Direction Feature Audit — {start_date} → {end_date}",
        f"",
        f"**Snapshots:** {n_docs}  ",
        f"**Gates (frozen):** CI_lb > {GATE_CI_LB} · pos_days ≥ {GATE_POS_DAYS_PCT*100:.0f}% · n ≥ {GATE_MIN_OBS}  ",
        f"**Bootstrap iterations:** {BOOTSTRAP_N} · seed {BOOTSTRAP_SEED}  ",
        f"",
    ]
    for horizon in HORIZONS_MIN:
        lines += [f"## Horizon +{horizon}m", ""]
        lines += ["| Feature | HitRate | CI_lb | PosDays | N_obs | Verdict |"]
        lines += ["|---------|---------|-------|---------|-------|---------|"]
        rows = [(f, r.get(horizon, {})) for f, r in results.items()]
        rows.sort(key=lambda x: x[1].get("ci_lb") or 0.0, reverse=True)
        for fname, r in rows:
            lines.append(
                f"| {fname} "
                f"| {_fmt_pct(r.get('hit_rate')).strip()} "
                f"| {_fmt_pct(r.get('ci_lb')).strip()} "
                f"| {_fmt_pct(r.get('pos_days_pct')).strip()} "
                f"| {r.get('n_obs', 0)} "
                f"| **{r.get('verdict', 'NO_DATA')}** |"
            )
        lines.append("")

    passes = [
        f for f, h in results.items()
        if any(r.get("verdict") == "PASS" for r in h.values())
    ]
    lines += [
        "## Summary",
        "",
        f"**Passed (≥1 horizon):** {', '.join(passes) if passes else 'none'}",
        "",
        "**Caveat:** Single-day n is too small to confirm pass — document and repeat weekly.",
        "",
        "---",
        f"*Generated {datetime.now(tz=IST).strftime('%Y-%m-%d %H:%M IST')}*",
    ]
    return "\n".join(lines)


# ── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="D1-S1 Direction feature audit framework")
    default_end = date.today().isoformat()
    default_start = (date.today() - timedelta(days=30)).isoformat()
    parser.add_argument("--start", default=default_start, help="Start date YYYY-MM-DD (default: 30 days ago)")
    parser.add_argument("--end", default=default_end, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--mongo",
        default=os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "mongodb://localhost:27017",
        help="MongoDB URI",
    )
    parser.add_argument("--db", default=os.getenv("MONGO_DB") or "trading_ai", help="Database name")
    parser.add_argument(
        "--collection",
        default=os.getenv("MONGO_COLL_SNAPSHOTS") or "phase1_market_snapshots",
        help="Snapshot collection name",
    )
    parser.add_argument("--save", default=None, help="Save markdown report to this path")
    parser.add_argument(
        "--features",
        default=None,
        help="Comma-separated feature names to test (default: all registered)",
    )
    args = parser.parse_args()

    feature_names = (
        [f.strip() for f in args.features.split(",") if f.strip()]
        if args.features
        else list(FEATURE_EXTRACTORS.keys())
    )

    print(f"Loading snapshots from {args.collection} ({args.start} → {args.end})...")
    docs = _load_snapshots(
        mongo_uri=args.mongo,
        db_name=args.db,
        collection=args.collection,
        start_date=args.start,
        end_date=args.end,
    )
    print(f"Loaded {len(docs)} snapshots across {len(set(d.get('trade_date_ist') for d in docs))} trading days.")

    if not docs:
        print("[FAIL] No snapshots found. Check date range and Mongo connection.")
        sys.exit(1)

    results = audit_features(docs, feature_names)
    _print_results(results, args.start, args.end, len(docs))

    if args.save:
        md = _results_to_markdown(results, args.start, args.end, len(docs))
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(md, encoding="utf-8")
        print(f"Report saved to: {args.save}")

    passes = [
        f for f, h in results.items()
        if any(r.get("verdict") == "PASS" for r in h.values())
    ]
    sys.exit(0 if passes else 1)


if __name__ == "__main__":
    main()
