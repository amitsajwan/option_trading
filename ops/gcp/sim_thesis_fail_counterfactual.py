#!/usr/bin/env python3
"""Counterfactual: what would net PF have been if we cut trades at bar 2 when
the thesis hadn't started working?

Joins per-bar POSITION_MANAGE events with POSITION_CLOSE outcomes, then for each
trade applies a candidate early-exit rule (using only data observable AT bar 2)
to produce an alternative close pnl. Reports net PF under each rule against the
true close.

Usage:
    python /tmp/sim_thesis_fail_counterfactual.py <run_id>
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


def load_trade_paths(db, run_id: str) -> list[dict]:
    """For each closed position, return:
       {position_id, direction, entry_premium, true_close_pnl_pct, true_exit_reason,
        bar2: {pnl_pct, mfe_pct, current_premium} or None}
    """
    # Pull every event for this run, grouped by position_id, sorted by timestamp.
    paths: dict[str, list[dict]] = defaultdict(list)
    for d in db.strategy_positions_historical.find(
        {"run_id": run_id},
        {"_id": 0, "position_id": 1, "event": 1, "timestamp": 1, "bars_held": 1,
         "pnl_pct": 1, "mfe_pct": 1, "current_premium": 1, "entry_premium": 1,
         "direction": 1, "exit_reason": 1, "market_time_ist": 1, "trade_date_ist": 1,
         "lots": 1, "exit_premium": 1},
    ):
        pid = d.get("position_id")
        if pid:
            paths[pid].append(d)

    out: list[dict] = []
    for pid, events in paths.items():
        events.sort(key=lambda x: x.get("timestamp") or "")
        close = next((e for e in events if e.get("event") == "POSITION_CLOSE"), None)
        open_ = next((e for e in events if e.get("event") == "POSITION_OPEN"), None)
        if not close or not open_:
            continue
        # bar 2 = the event with bars_held==2 (POSITION_MANAGE)
        bar2 = next(
            (e for e in events if int(e.get("bars_held") or -1) == 2 and e.get("event") == "POSITION_MANAGE"),
            None,
        )
        out.append({
            "position_id": pid,
            "date": str(close.get("trade_date_ist") or ""),
            "market_time": str(open_.get("market_time_ist") or ""),
            "direction": str(close.get("direction") or ""),
            "entry_premium": float(close.get("entry_premium") or 0),
            "exit_premium": float(close.get("exit_premium") or 0),
            "lots": max(1, int(close.get("lots") or 1)),
            "true_close_pnl_pct": float(close.get("pnl_pct") or 0),
            "true_exit_reason": str(close.get("exit_reason") or ""),
            "true_mfe_pct": float(close.get("mfe_pct") or 0),
            "bars_held": int(close.get("bars_held") or 0),
            "bar2": ({
                "pnl_pct": float(bar2.get("pnl_pct") or 0),
                "mfe_pct": float(bar2.get("mfe_pct") or 0),
                "current_premium": float(bar2.get("current_premium") or 0),
            } if bar2 else None),
        })
    return out


def _in_window(market_time: str, windows: list[tuple[str, str]] | None) -> bool:
    if not windows:
        return True
    if not market_time or len(market_time) < 5:
        return False
    try:
        hh, mm = int(market_time[:2]), int(market_time[3:5])
    except ValueError:
        return False
    mins = hh * 60 + mm
    for start, end in windows:
        sh, sm = int(start[:2]), int(start[3:5])
        eh, em = int(end[:2]), int(end[3:5])
        if sh * 60 + sm <= mins < eh * 60 + em:
            return True
    return False


def apply_rule(
    trade: dict,
    *,
    mfe_threshold: float,
    require_loss: bool,
    only_in_windows: list[tuple[str, str]] | None = None,
) -> dict:
    """Return a row with `close_pnl_pct` overridden by counterfactual if rule fires.

    Rule fires at bar 2 if mfe_pct < mfe_threshold and (optionally) pnl_pct < 0,
    optionally restricted to entries in the listed time windows.
    When rule fires, replace close pnl with bar-2 pnl.
    Trades with no bar-2 event (closed before bar 2) are left untouched.
    """
    bar2 = trade.get("bar2")
    fired = False
    close_pnl = trade["true_close_pnl_pct"]
    close_premium = trade["exit_premium"]
    in_window = _in_window(trade.get("market_time", ""), only_in_windows)
    if bar2 is not None and in_window:
        if bar2["mfe_pct"] < mfe_threshold and (not require_loss or bar2["pnl_pct"] < 0):
            fired = True
            close_pnl = bar2["pnl_pct"]
            close_premium = bar2["current_premium"]
    units = trade["lots"] * LOT_SIZE
    entry_value = trade["entry_premium"] * units
    exit_value = close_premium * units
    gross = exit_value - entry_value
    cost = _trade_costs(entry_value, exit_value)
    net = gross - cost
    net_pnl_pct = (net / entry_value) if entry_value > 0 else 0.0
    net_cap_pnl_pct = (net / CAPITAL) if CAPITAL > 0 else 0.0
    return {
        **trade,
        "rule_fired": fired,
        "close_pnl_pct": close_pnl,
        "net_pnl_pct": net_pnl_pct,
        "net_cap_pnl_pct": net_cap_pnl_pct,
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
    return (pfs[int(0.025 * len(pfs))], pfs[len(pfs)//2], pfs[min(len(pfs)-1, int(0.975*len(pfs)))])


def summarize(label: str, rows: list[dict]) -> None:
    fired = [r for r in rows if r.get("rule_fired")]
    n = len(rows)
    lo, med, hi = bootstrap_pf_ci(rows)
    net = pf(rows)
    ce = [r for r in rows if r["direction"] == "CE"]
    pe = [r for r in rows if r["direction"] == "PE"]
    avg_net = mean(r["net_pnl_pct"] for r in rows) * 100 if rows else 0.0
    fired_pct = (len(fired) / n * 100) if n else 0.0
    saved_pnl = sum((r["true_close_pnl_pct"] - r["close_pnl_pct"]) for r in fired)
    print(
        f"  {label:<35} net PF={net:.3f}  CI=[{lo:.2f},{hi:.2f}]  "
        f"CE_PF={pf(ce):.2f}  PE_PF={pf(pe):.2f}  "
        f"avg_net={avg_net:+.2f}%  rule_fired={len(fired)}/{n} ({fired_pct:.0f}%)  "
        f"sum_diff_vs_actual_pnl={saved_pnl*100:+.1f}pp"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    args = ap.parse_args()

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    db = client[DB_NAME]
    trades = load_trade_paths(db, args.run_id)
    if not trades:
        print(f"no trades for {args.run_id}", file=sys.stderr)
        return 1

    with_bar2 = [t for t in trades if t.get("bar2") is not None]
    no_bar2 = len(trades) - len(with_bar2)

    print(f"# Counterfactual thesis-fail simulation — run_id {args.run_id}")
    print(f"\nTotal closed trades: {len(trades)}  (with bar-2 data: {len(with_bar2)}, without: {no_bar2})\n")

    print("## Baseline (no rule)")
    baseline = [apply_rule(t, mfe_threshold=-1.0, require_loss=False) for t in trades]
    summarize("baseline", baseline)

    print("\n## Counterfactual exit rules")
    print("  Rule fires at bar 2 -> replace exit pnl with bar-2 pnl\n")
    rules = [
        ("A: mfe<0.5% AND pnl<0",   {"mfe_threshold": 0.005, "require_loss": True}),
        ("B: mfe<1.0% AND pnl<0",   {"mfe_threshold": 0.010, "require_loss": True}),
        ("C: mfe<1.0% (any sign)",  {"mfe_threshold": 0.010, "require_loss": False}),
        ("D: mfe<2.0% AND pnl<0",   {"mfe_threshold": 0.020, "require_loss": True}),
        ("E: mfe<2.0% (any sign)",  {"mfe_threshold": 0.020, "require_loss": False}),
        ("F: mfe<3.0% AND pnl<0",   {"mfe_threshold": 0.030, "require_loss": True}),
    ]
    for label, kwargs in rules:
        rows = [apply_rule(t, **kwargs) for t in trades]
        summarize(label, rows)

    print("\n## Same rules applied to CE-only book")
    ce_trades = [t for t in trades if t["direction"] == "CE"]
    print(f"  (CE n={len(ce_trades)})\n")
    summarize("CE baseline", [apply_rule(t, mfe_threshold=-1.0, require_loss=False) for t in ce_trades])
    for label, kwargs in rules:
        rows = [apply_rule(t, **kwargs) for t in ce_trades]
        summarize(f"CE {label}", rows)

    # ------------------------------------------------------------------
    # Time-conditional fast cut: only fire bar-2 exit in dead-zone windows
    # identified from Ref decomp (CE PF < 0.5 there).
    # ------------------------------------------------------------------
    dead_zones = [("12:15", "12:45"), ("14:15", "14:45")]
    print("\n## Time-conditional bar-2 cut (only fires in dead-zone windows 12:15-12:45 and 14:15-14:45)")
    for label, kwargs in rules[:3]:
        rows = [apply_rule(t, **kwargs, only_in_windows=dead_zones) for t in trades]
        summarize(f"dead-zone-only {label}", rows)
    for label, kwargs in rules[:3]:
        rows = [apply_rule(t, **kwargs, only_in_windows=dead_zones) for t in ce_trades]
        summarize(f"CE dead-zone-only {label}", rows)

    # ------------------------------------------------------------------
    # Top-window entry restriction: keep only trades opened in top-3 windows
    # from Ref decomp (10:45-11:15, 09:45-10:15, 11:15-11:45). No exit
    # changes — pure entry-side filter.
    # ------------------------------------------------------------------
    top_windows = [("09:45", "10:15"), ("10:45", "11:15"), ("11:15", "11:45")]
    print("\n## Top-window entry-only filter (entries restricted to 09:45-10:15, 10:45-11:15, 11:15-11:45)")
    top_full = [apply_rule(t, mfe_threshold=-1.0, require_loss=False)
                for t in trades if _in_window(t.get("market_time", ""), top_windows)]
    top_ce = [apply_rule(t, mfe_threshold=-1.0, require_loss=False)
              for t in ce_trades if _in_window(t.get("market_time", ""), top_windows)]
    summarize("top-windows (all sides)", top_full)
    summarize("top-windows CE-only", top_ce)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
