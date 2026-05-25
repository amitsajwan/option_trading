#!/usr/bin/env python3
"""E8 — daily-regime-conditional CE/PE bias counterfactual.

For each trade date in the run, derive a regime tag from the 09:15 (and 09:45)
snapshots, then bucket the trade book by {regime × direction} and compute net PF
per cell. Goal: surface whether CE/PE edge varies by daily regime — addressing
the May/Jun/Jul direction-mix flip that no per-bar consensus can fix.

Outputs:
  - Per-regime-scheme breakdown: cell counts, net PF, bootstrap CI
  - Stacked with top-3 time windows from E7 finding

Usage (inside dashboard container):
    python /tmp/sim_regime_conditional.py <run_id>
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from collections import defaultdict
from statistics import mean

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo required", file=sys.stderr)
    sys.exit(2)

MONGO_URL = os.getenv("MONGO_URL", "mongodb://mongo:27017")
DB_NAME = os.getenv("MONGO_DB", "trading_ai")
LOT_SIZE = 15
CAPITAL = 100_000

COST_BROKERAGE_PER_ORDER = float(os.getenv("OOS_COST_BROKERAGE_PER_ORDER", "20.0"))
COST_CHARGES_BPS = float(os.getenv("OOS_COST_CHARGES_BPS", "2.5"))
COST_SLIPPAGE_BPS = float(os.getenv("OOS_COST_SLIPPAGE_BPS", "7.5"))


def _trade_costs(entry_value: float, exit_value: float) -> float:
    brokerage = 2.0 * COST_BROKERAGE_PER_ORDER
    bps_rate = (COST_CHARGES_BPS + COST_SLIPPAGE_BPS) / 10000.0
    return brokerage + (max(0.0, entry_value) + max(0.0, exit_value)) * bps_rate


def load_trades(db, run_id: str) -> list[dict]:
    rows: list[dict] = []
    for d in db.strategy_positions_historical.find(
        {"run_id": run_id, "event": "POSITION_CLOSE"},
        {"_id": 0, "trade_date_ist": 1, "market_time_ist": 1, "direction": 1,
         "exit_reason": 1, "pnl_pct": 1, "entry_premium": 1, "exit_premium": 1, "lots": 1,
         "mfe_pct": 1},
    ):
        pnl_pct = float(d.get("pnl_pct") or 0)
        entry_prem = float(d.get("entry_premium") or 0)
        exit_prem = float(d.get("exit_premium") or (entry_prem * (1.0 + pnl_pct)))
        lots = max(1, int(d.get("lots") or 1))
        units = lots * LOT_SIZE
        entry_value = entry_prem * units
        exit_value = exit_prem * units
        gross = exit_value - entry_value
        cost = _trade_costs(entry_value, exit_value)
        net = gross - cost
        rows.append({
            "date": str(d.get("trade_date_ist") or ""),
            "market_time": str(d.get("market_time_ist") or ""),
            "direction": str(d.get("direction") or ""),
            "exit_reason": str(d.get("exit_reason") or ""),
            "net_pnl_pct": (net / entry_value) if entry_value > 0 else 0.0,
            "net_cap_pnl_pct": (net / CAPITAL) if CAPITAL > 0 else 0.0,
            "mfe_pct": float(d.get("mfe_pct") or 0),
        })
    return rows


def load_regime_features(db, dates: list[str]) -> dict[str, dict]:
    """For each date, pull 09:15 + 09:45 snapshots and extract regime features."""
    features: dict[str, dict] = {}
    for date in dates:
        snap_915 = db.phase1_market_snapshots_historical.find_one(
            {"trade_date_ist": date, "market_time_ist": "09:15:00"}
        )
        snap_945 = db.phase1_market_snapshots_historical.find_one(
            {"trade_date_ist": date, "market_time_ist": "09:45:00"}
        )
        if not snap_915:
            continue
        s915 = (snap_915.get("payload") or {}).get("snapshot") or {}
        sl = s915.get("session_levels") or {}
        fb = s915.get("futures_bar") or {}
        # 09:45 enrichment
        orb_dir = "none"
        fut_at_945 = None
        if snap_945:
            s945 = (snap_945.get("payload") or {}).get("snapshot") or {}
            or_ = s945.get("opening_range") or {}
            fb945 = s945.get("futures_bar") or {}
            fut_at_945 = fb945.get("fut_close")
            if or_.get("orh_broken"):
                orb_dir = "up"
            elif or_.get("orl_broken"):
                orb_dir = "down"
        prev_close = sl.get("prev_day_close")
        fut_open = fb.get("fut_open")
        open_vs_prev = None
        if prev_close and fut_open:
            open_vs_prev = (float(fut_open) - float(prev_close)) / float(prev_close)
        features[date] = {
            "overnight_gap": sl.get("overnight_gap"),
            "open_vs_prev": open_vs_prev,
            "prev_day_pcr": sl.get("prev_day_pcr"),
            "prev_day_high": sl.get("prev_day_high"),
            "prev_day_low": sl.get("prev_day_low"),
            "fut_open": fut_open,
            "fut_at_945": fut_at_945,
            "orb_dir_at_945": orb_dir,
        }
    return features


def tag_simple_gap(feats: dict, threshold: float = 0.003) -> str:
    g = feats.get("overnight_gap")
    if g is None:
        return "unknown"
    if g > threshold:
        return "bull"
    if g < -threshold:
        return "bear"
    return "chop"


def tag_open_vs_prev(feats: dict, threshold: float = 0.002) -> str:
    v = feats.get("open_vs_prev")
    if v is None:
        return "unknown"
    if v > threshold:
        return "bull"
    if v < -threshold:
        return "bear"
    return "chop"


def tag_orb_direction(feats: dict) -> str:
    d = feats.get("orb_dir_at_945")
    if d == "up":
        return "bull"
    if d == "down":
        return "bear"
    return "chop"


def tag_pcr(feats: dict) -> str:
    pcr = feats.get("prev_day_pcr")
    if pcr is None:
        return "unknown"
    # Low PCR = bullish (more call open interest)
    if pcr < 0.7:
        return "bull"
    if pcr > 1.2:
        return "bear"
    return "chop"


def tag_combined(feats: dict) -> str:
    """Majority vote across gap, open_vs_prev, orb_direction. PCR as tiebreak."""
    votes = [
        tag_simple_gap(feats, threshold=0.002),
        tag_open_vs_prev(feats, threshold=0.0015),
        tag_orb_direction(feats),
    ]
    bull = sum(1 for v in votes if v == "bull")
    bear = sum(1 for v in votes if v == "bear")
    if bull >= 2:
        return "bull"
    if bear >= 2:
        return "bear"
    # tie-break on PCR
    pcr_tag = tag_pcr(feats)
    if pcr_tag in ("bull", "bear"):
        return pcr_tag
    return "chop"


TAGGERS = {
    "gap_03pct":   lambda f: tag_simple_gap(f, 0.003),
    "open_vs_prev_02pct": lambda f: tag_open_vs_prev(f, 0.002),
    "orb_at_945":  tag_orb_direction,
    "pcr_prev_day": tag_pcr,
    "combined_majority": tag_combined,
}


def pf(rows: list[dict], *, key="net_cap_pnl_pct", sign_key="net_pnl_pct") -> float:
    wins = sum(r[key] for r in rows if r[sign_key] > 0)
    loss = abs(sum(r[key] for r in rows if r[sign_key] <= 0))
    if loss <= 0:
        return float("inf") if wins > 0 else 0.0
    return wins / loss


def bootstrap_pf_ci(rows: list[dict], *, iterations=1500, seed=42) -> tuple[float, float, float]:
    if not rows:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(rows)
    pfs = []
    for _ in range(iterations):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
        p = pf(sample)
        if p == float("inf"):
            continue
        pfs.append(p)
    if not pfs:
        return (0.0, 0.0, 0.0)
    pfs.sort()
    return (pfs[int(0.025*len(pfs))], pfs[len(pfs)//2], pfs[min(len(pfs)-1, int(0.975*len(pfs)))])


def _in_top_windows(market_time: str) -> bool:
    if not market_time or len(market_time) < 5:
        return False
    hh, mm = int(market_time[:2]), int(market_time[3:5])
    mins = hh * 60 + mm
    return (
        (9*60+45 <= mins < 10*60+15) or
        (10*60+45 <= mins < 11*60+15) or
        (11*60+15 <= mins < 11*60+45)
    )


def cell_table(label: str, trades: list[dict], tagger_name: str, regime_by_date: dict[str, str]) -> None:
    print(f"\n## {label}  (tagger: {tagger_name})")
    print()
    print("| regime × dir | n | net_PF | bootstrap CI | avg_net | %winners |")
    print("|--------------|---|--------|--------------|---------|----------|")
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        tag = regime_by_date.get(t["date"], "unknown")
        grouped[(tag, t["direction"])].append(t)
    # Sort regime then direction
    for (tag, dirn) in sorted(grouped.keys()):
        rows = grouped[(tag, dirn)]
        n = len(rows)
        lo, _, hi = bootstrap_pf_ci(rows)
        wr = sum(1 for r in rows if r["net_pnl_pct"] > 0) / n * 100 if n else 0
        avg = mean(r["net_pnl_pct"] for r in rows) * 100 if n else 0
        print(f"| {tag:<5} × {dirn:<2} | {n} | {pf(rows):.2f} | [{lo:.2f}, {hi:.2f}] | {avg:+.2f}% | {wr:.0f}% |")
    # Combined CE-bull / PE-bear stacked (the "ideal regime trade")
    combined = []
    for t in trades:
        tag = regime_by_date.get(t["date"], "unknown")
        if (tag == "bull" and t["direction"] == "CE") or (tag == "bear" and t["direction"] == "PE"):
            combined.append(t)
    if combined:
        lo, _, hi = bootstrap_pf_ci(combined)
        wr = sum(1 for r in combined if r["net_pnl_pct"] > 0) / len(combined) * 100
        print(f"| **CE-bull + PE-bear stacked** | **{len(combined)}** | **{pf(combined):.2f}** | **[{lo:.2f}, {hi:.2f}]** | {mean(r['net_pnl_pct'] for r in combined)*100:+.2f}% | {wr:.0f}% |")
    # CE-only-on-bull (the asymmetric version: keep CE in bull, drop PE entirely)
    ce_bull = [t for t in trades if regime_by_date.get(t["date"]) == "bull" and t["direction"] == "CE"]
    if ce_bull:
        lo, _, hi = bootstrap_pf_ci(ce_bull)
        wr = sum(1 for r in ce_bull if r["net_pnl_pct"] > 0) / len(ce_bull) * 100
        print(f"| **CE-only on bull days** | **{len(ce_bull)}** | **{pf(ce_bull):.2f}** | **[{lo:.2f}, {hi:.2f}]** | {mean(r['net_pnl_pct'] for r in ce_bull)*100:+.2f}% | {wr:.0f}% |")
    # CE-bull + top-3 windows stack
    ce_bull_topw = [t for t in ce_bull if _in_top_windows(t["market_time"])]
    if ce_bull_topw:
        lo, _, hi = bootstrap_pf_ci(ce_bull_topw)
        wr = sum(1 for r in ce_bull_topw if r["net_pnl_pct"] > 0) / len(ce_bull_topw) * 100
        print(f"| **CE-only × bull days × top-3 windows** | **{len(ce_bull_topw)}** | **{pf(ce_bull_topw):.2f}** | **[{lo:.2f}, {hi:.2f}]** | {mean(r['net_pnl_pct'] for r in ce_bull_topw)*100:+.2f}% | {wr:.0f}% |")
    # INVERTED hypothesis: CE on NOT-bull (bear + chop) days — counterintuitive but data-driven
    ce_not_bull = [t for t in trades if regime_by_date.get(t["date"]) in ("bear", "chop") and t["direction"] == "CE"]
    if ce_not_bull:
        lo, _, hi = bootstrap_pf_ci(ce_not_bull)
        wr = sum(1 for r in ce_not_bull if r["net_pnl_pct"] > 0) / len(ce_not_bull) * 100
        print(f"| **CE × NOT-bull (bear+chop)** | **{len(ce_not_bull)}** | **{pf(ce_not_bull):.2f}** | **[{lo:.2f}, {hi:.2f}]** | {mean(r['net_pnl_pct'] for r in ce_not_bull)*100:+.2f}% | {wr:.0f}% |")
    ce_not_bull_topw = [t for t in ce_not_bull if _in_top_windows(t["market_time"])]
    if ce_not_bull_topw:
        lo, _, hi = bootstrap_pf_ci(ce_not_bull_topw)
        wr = sum(1 for r in ce_not_bull_topw if r["net_pnl_pct"] > 0) / len(ce_not_bull_topw) * 100
        print(f"| **CE × NOT-bull × top-3 windows** | **{len(ce_not_bull_topw)}** | **{pf(ce_not_bull_topw):.2f}** | **[{lo:.2f}, {hi:.2f}]** | {mean(r['net_pnl_pct'] for r in ce_not_bull_topw)*100:+.2f}% | {wr:.0f}% |")
    # CE × bear only (the strongest single cell across taggers)
    ce_bear = [t for t in trades if regime_by_date.get(t["date"]) == "bear" and t["direction"] == "CE"]
    if ce_bear:
        ce_bear_topw = [t for t in ce_bear if _in_top_windows(t["market_time"])]
        if ce_bear_topw:
            lo, _, hi = bootstrap_pf_ci(ce_bear_topw)
            wr = sum(1 for r in ce_bear_topw if r["net_pnl_pct"] > 0) / len(ce_bear_topw) * 100
            print(f"| **CE × bear-only × top-3 windows** | **{len(ce_bear_topw)}** | **{pf(ce_bear_topw):.2f}** | **[{lo:.2f}, {hi:.2f}]** | {mean(r['net_pnl_pct'] for r in ce_bear_topw)*100:+.2f}% | {wr:.0f}% |")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    args = ap.parse_args()

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    db = client[DB_NAME]
    trades = load_trades(db, args.run_id)
    if not trades:
        print(f"no trades for {args.run_id}", file=sys.stderr)
        return 1

    dates = sorted({t["date"] for t in trades if t["date"]})
    feats = load_regime_features(db, dates)
    print(f"# E8 regime-conditional analysis — run {args.run_id}")
    print(f"\nTrades: {len(trades)}  trade_dates: {len(dates)}  regime_features_resolved: {len(feats)}")

    # Show regime distribution per tagger
    print("\n## Regime distribution per tagger")
    for name, fn in TAGGERS.items():
        counts: dict[str, int] = defaultdict(int)
        for d in dates:
            f = feats.get(d) or {}
            counts[fn(f)] += 1
        print(f"  {name:<22} {dict(counts)}")

    # Baseline reference
    from collections import Counter
    ce = [t for t in trades if t["direction"] == "CE"]
    pe = [t for t in trades if t["direction"] == "PE"]
    lo, _, hi = bootstrap_pf_ci(trades)
    print(f"\nBaseline full book: n={len(trades)} net_PF={pf(trades):.3f} CI=[{lo:.2f},{hi:.2f}]")
    lo, _, hi = bootstrap_pf_ci(ce)
    print(f"Baseline CE-only:   n={len(ce)} net_PF={pf(ce):.3f} CI=[{lo:.2f},{hi:.2f}]")
    lo, _, hi = bootstrap_pf_ci(pe)
    print(f"Baseline PE-only:   n={len(pe)} net_PF={pf(pe):.3f} CI=[{lo:.2f},{hi:.2f}]")

    # Tables per tagger
    for name, fn in TAGGERS.items():
        regime_by_date = {d: fn(feats.get(d) or {}) for d in dates}
        cell_table(f"Cells per regime × direction", trades, name, regime_by_date)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
