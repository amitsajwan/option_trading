#!/usr/bin/env python3
"""Decompose a replay run by time-of-day, month, ml_entry_prob, and "had-life".

Goal: turn the human-style spec's guessed buckets (time windows, setup families)
into measured ones. Output is markdown-friendly tables.

Usage (inside dashboard container):
    python /tmp/analyze_run_decomposition.py <run_id> [--min-bucket-n 20]

Reads:  strategy_positions_historical {run_id, event=POSITION_CLOSE}
Writes: stdout only.

Setup-family tagging is intentionally NOT done here — that requires joining
vote.raw_signals which is missing for 78% of trades (NO_ML_VOTE_MATCH gap).
This script uses only fields persisted on the close doc.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from collections import defaultdict
from statistics import mean, median

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo required", file=sys.stderr)
    sys.exit(2)

MONGO_URL = os.getenv("MONGO_URL", "mongodb://mongo:27017")
DB_NAME = os.getenv("MONGO_DB", "trading_ai")
LOT_SIZE = 15
CAPITAL = 100_000

# Cost overlay — keep aligned with analyze_oos_validation_run.py defaults
COST_BROKERAGE_PER_ORDER = float(os.getenv("OOS_COST_BROKERAGE_PER_ORDER", "20.0"))
COST_CHARGES_BPS = float(os.getenv("OOS_COST_CHARGES_BPS", "2.5"))
COST_SLIPPAGE_BPS = float(os.getenv("OOS_COST_SLIPPAGE_BPS", "7.5"))


def _trade_costs(entry_value: float, exit_value: float) -> float:
    safe_entry = max(0.0, entry_value)
    safe_exit = max(0.0, exit_value)
    brokerage = 2.0 * COST_BROKERAGE_PER_ORDER
    bps_rate = (COST_CHARGES_BPS + COST_SLIPPAGE_BPS) / 10000.0
    return brokerage + (safe_entry + safe_exit) * bps_rate


def load_trades(db, run_id: str) -> list[dict]:
    rows: list[dict] = []
    for doc in db.strategy_positions_historical.find(
        {"run_id": run_id, "event": "POSITION_CLOSE"},
        {"_id": 0},
    ):
        pnl_pct = float(doc.get("pnl_pct") or 0)
        entry_prem = float(doc.get("entry_premium") or 0)
        exit_prem = float(doc.get("exit_premium") or (entry_prem * (1.0 + pnl_pct)))
        lots = max(1, int(doc.get("lots") or 1))
        units = lots * LOT_SIZE
        entry_value = entry_prem * units
        exit_value = exit_prem * units
        gross = exit_value - entry_value if str(doc.get("direction") or "") != "SHORT" else entry_value - exit_value
        cost = _trade_costs(entry_value, exit_value)
        net = gross - cost
        net_pnl_pct = (net / entry_value) if entry_value > 0 else 0.0
        net_cap_pnl_pct = (net / CAPITAL) if CAPITAL > 0 else 0.0
        cap_pnl_pct = (pnl_pct * entry_value) / CAPITAL if CAPITAL > 0 else 0.0
        rows.append({
            "date": str(doc.get("trade_date_ist") or ""),
            "market_time": str(doc.get("market_time_ist") or ""),
            "direction": str(doc.get("direction") or ""),
            "exit_reason": str(doc.get("exit_reason") or ""),
            "pnl_pct": pnl_pct,
            "net_pnl_pct": net_pnl_pct,
            "cap_pnl_pct": cap_pnl_pct,
            "net_cap_pnl_pct": net_cap_pnl_pct,
            "ml_entry_prob": float(doc.get("ml_entry_prob") or 0.0),
            "mfe_pct": float(doc.get("mfe_pct") or 0.0),
            "mae_pct": float(doc.get("mae_pct") or 0.0),
            "bars_held": int(doc.get("bars_held") or 0),
            "entry_premium": entry_prem,
        })
    return rows


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
    lo = pfs[int(0.025 * len(pfs))]
    hi = pfs[min(len(pfs) - 1, int(0.975 * len(pfs)))]
    med = pfs[len(pfs) // 2]
    return (lo, med, hi)


def time_bucket(market_time: str) -> str:
    """Bucket HH:MM:SS into 30-min IST windows (NSE session 09:15-15:30)."""
    if not market_time or len(market_time) < 5:
        return "unknown"
    try:
        hh, mm = int(market_time[:2]), int(market_time[3:5])
    except ValueError:
        return "unknown"
    mins = hh * 60 + mm
    # 30-min buckets starting at 09:15
    start = 9 * 60 + 15
    if mins < start:
        return "pre_open"
    if mins >= 15 * 60 + 30:
        return "post_close"
    bucket_idx = (mins - start) // 30
    bstart = start + bucket_idx * 30
    bend = bstart + 30
    return f"{bstart//60:02d}:{bstart%60:02d}-{bend//60:02d}:{bend%60:02d}"


def ml_prob_bucket(p: float) -> str:
    if p < 0.65:
        return "<0.65"
    if p < 0.70:
        return "0.65-0.70"
    if p < 0.75:
        return "0.70-0.75"
    if p < 0.80:
        return "0.75-0.80"
    if p < 0.85:
        return "0.80-0.85"
    return ">=0.85"


def life_bucket(mfe_pct: float) -> str:
    """Did the trade ever run? Proxy for early confirmation in §9 of spec."""
    if mfe_pct < 0.02:
        return "no_life (mfe<2%)"
    if mfe_pct < 0.05:
        return "weak (2-5%)"
    if mfe_pct < 0.10:
        return "moderate (5-10%)"
    if mfe_pct < 0.20:
        return "strong (10-20%)"
    return "runner (>=20%)"


def print_table(title: str, rows_by_bucket: dict[str, list[dict]], min_n: int) -> None:
    print(f"\n## {title}")
    print()
    print("| Bucket | n | CE_n | PE_n | net_PF | CE_PF | PE_PF | avg_net% | %TIME_STOP | %winners |")
    print("|--------|---|------|------|--------|-------|-------|----------|------------|----------|")
    # Sort by bucket name (lexical works for time buckets and prob buckets)
    for bucket in sorted(rows_by_bucket.keys()):
        rows = rows_by_bucket[bucket]
        n = len(rows)
        if n < 1:
            continue
        ce = [r for r in rows if r["direction"] == "CE"]
        pe = [r for r in rows if r["direction"] == "PE"]
        nets = [r["net_pnl_pct"] for r in rows]
        ts_pct = sum(1 for r in rows if r["exit_reason"] == "TIME_STOP") / n * 100
        win_pct = sum(1 for r in rows if r["net_pnl_pct"] > 0) / n * 100
        ce_pf_str = f"{pf(ce):.2f}" if ce else "-"
        pe_pf_str = f"{pf(pe):.2f}" if pe else "-"
        net_pf_str = f"{pf(rows):.2f}"
        flag = " " if n >= min_n else " *"  # mark thin buckets
        print(
            f"| {bucket}{flag} | {n} | {len(ce)} | {len(pe)} | "
            f"{net_pf_str} | {ce_pf_str} | {pe_pf_str} | "
            f"{mean(nets)*100:+.2f}% | {ts_pct:.0f}% | {win_pct:.0f}% |"
        )
    print(f"\n_(buckets marked `*` have n < {min_n} — read with caution)_")


def print_overall(rows: list[dict]) -> None:
    n = len(rows)
    print(f"\n## Overall")
    print(f"- trades: **{n}**")
    print(f"- gross PF: {pf(rows, key='cap_pnl_pct', sign_key='pnl_pct'):.3f}")
    net = pf(rows)
    lo, med, hi = bootstrap_pf_ci(rows)
    print(f"- **net PF: {net:.3f}** (bootstrap 95% CI: [{lo:.2f}, {hi:.2f}], n_iter=1500)")
    ce = [r for r in rows if r["direction"] == "CE"]
    pe = [r for r in rows if r["direction"] == "PE"]
    print(f"- CE: n={len(ce)}, net PF={pf(ce):.2f}, avg_net={mean(r['net_pnl_pct'] for r in ce)*100:+.2f}%")
    print(f"- PE: n={len(pe)}, net PF={pf(pe):.2f}, avg_net={mean(r['net_pnl_pct'] for r in pe)*100:+.2f}%")
    print(
        f"- cost overlay: brokerage={COST_BROKERAGE_PER_ORDER:.1f}/order  "
        f"charges={COST_CHARGES_BPS:.1f}bps/side  slippage={COST_SLIPPAGE_BPS:.1f}bps/side"
    )


def suggest_top_buckets(by_time: dict[str, list[dict]], min_n: int) -> None:
    print("\n## Suggested time-window filter (top-3 buckets by CE net PF, n >= {})".format(min_n))
    candidates = []
    for bucket, rows in by_time.items():
        ce = [r for r in rows if r["direction"] == "CE"]
        if len(ce) >= min_n:
            candidates.append((bucket, pf(ce), len(ce)))
    candidates.sort(key=lambda x: x[1], reverse=True)
    for bucket, ce_pf, n_ce in candidates[:5]:
        print(f"- **{bucket}**  CE net PF {ce_pf:.2f}  (n={n_ce})")
    if not candidates:
        print("- no buckets met the n threshold")
    # Total trades that would survive a top-3 filter
    top3 = {b for b, _, _ in candidates[:3]}
    surviving = sum(len(rows) for b, rows in by_time.items() if b in top3)
    surviving_ce = sum(len([r for r in rows if r['direction'] == 'CE']) for b, rows in by_time.items() if b in top3)
    print(f"\n- if engine restricted to top-3 windows: **{surviving} total trades** (CE: {surviving_ce})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--min-bucket-n", type=int, default=20)
    args = ap.parse_args()

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    db = client[DB_NAME]
    rows = load_trades(db, args.run_id)
    if not rows:
        print(f"no POSITION_CLOSE rows for run_id={args.run_id}", file=sys.stderr)
        return 1

    print(f"# Decomposition — run_id `{args.run_id}`")
    print_overall(rows)

    by_time: dict[str, list[dict]] = defaultdict(list)
    by_month: dict[str, list[dict]] = defaultdict(list)
    by_prob: dict[str, list[dict]] = defaultdict(list)
    by_life: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_time[time_bucket(r["market_time"])].append(r)
        by_month[r["date"][:7] or "unknown"].append(r)
        by_prob[ml_prob_bucket(r["ml_entry_prob"])].append(r)
        by_life[life_bucket(r["mfe_pct"])].append(r)

    print_table("Time-of-day (30-min IST buckets)", by_time, args.min_bucket_n)
    print_table("Month", by_month, args.min_bucket_n)
    print_table("ML entry prob bucket", by_prob, args.min_bucket_n)
    print_table("Trade life (MFE bucket — proxy for 'had-life' confirmation)", by_life, args.min_bucket_n)
    suggest_top_buckets(by_time, args.min_bucket_n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
