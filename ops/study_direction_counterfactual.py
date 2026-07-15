#!/usr/bin/env python3
"""For each closed trade in a given replay run, look up the OPPOSITE side's
real premium at the same entry/exit bars and strike, and compute what P&L
that side would have shown. Answers: was the loss a direction problem
(opposite side would have won) or a timing/thesis problem (opposite side
also flat/lost)? (2026-07-15, user request.)

Usage: python ops/study_direction_counterfactual.py "replay-multiday-2026-06-0[1-4]-banknifty" BANKNIFTY
"""
import re
import sys
from pymongo import MongoClient

run_id_pattern = sys.argv[1] if len(sys.argv) > 1 else "replay-multiday-2026-06-0[1-4]-banknifty"
instrument = sys.argv[2].upper() if len(sys.argv) > 2 else "BANKNIFTY"
COLL = "phase1_market_snapshots_hist" if instrument == "BANKNIFTY" else "phase1_market_snapshots_hist_nifty"

db = MongoClient("mongo", 27017).trading_ai
pos_coll = db.strategy_positions_historical
snap_coll = db[COLL]

closes = list(pos_coll.find(
    {"exit_reason": {"$ne": None}, "run_id": {"$regex": f"^{run_id_pattern}$"}}
).sort("timestamp", 1))

seen = set()
trades = []
for p in closes:
    pos = (p.get("payload") or {}).get("position") or {}
    pid = pos.get("position_id")
    if not pid or pid in seen:
        continue
    seen.add(pid)
    trades.append({
        "date": p.get("trade_date_ist"),
        "strike": p.get("strike"),
        "direction": p.get("direction"),
        "entry_snap": pos.get("entry_snapshot_id"),
        "exit_snap": pos.get("snapshot_id"),
        "entry_premium": pos.get("entry_premium"),
        "exit_premium": pos.get("exit_premium"),
        "pnl_pct": p.get("pnl_pct"),
        "exit_reason": p.get("exit_reason"),
        "entry_regime": pos.get("entry_regime"),
    })

print(f"{len(trades)} distinct closed trades\n")


def strike_row(snapshot_id, strike):
    doc = snap_coll.find_one({"snapshot_id": snapshot_id})
    if not doc:
        return None
    snap = (doc.get("payload") or {}).get("snapshot") or {}
    for row in (snap.get("strikes") or []):
        if row.get("strike") == strike:
            return row
    return None


total_actual = 0.0
total_opposite = 0.0
n_would_flip_win = 0
n_priced = 0
print(f"{'date':>10} {'strike':>7} {'dir':>4} {'actual_pnl':>11} {'opp_pnl':>9} {'opp_would_win?':>15} {'exit_reason':>14}")
for t in trades:
    entry_row = strike_row(t["entry_snap"], t["strike"])
    exit_row = strike_row(t["exit_snap"], t["strike"])
    opp_side = "pe" if t["direction"] == "CE" else "ce"
    opp_pnl = None
    if entry_row and exit_row:
        e = entry_row.get(f"{opp_side}_ltp")
        x = exit_row.get(f"{opp_side}_ltp")
        if e is not None and x is not None and e > 0:
            opp_pnl = (x - e) / e

    actual_pct = t["pnl_pct"] * 100 if t["pnl_pct"] is not None else float("nan")
    if opp_pnl is not None:
        opp_pct = opp_pnl * 100
        total_actual += actual_pct
        total_opposite += opp_pct
        n_priced += 1
        would_flip = "YES" if (actual_pct < 0 and opp_pct > 0) else ("no" if actual_pct < 0 else "n/a(won)")
        if would_flip == "YES":
            n_would_flip_win += 1
        print(f"{t['date']:>10} {t['strike']:>7} {t['direction']:>4} {actual_pct:10.2f}% {opp_pct:8.2f}% "
              f"{would_flip:>15} {t['exit_reason']:>14}")
    else:
        print(f"{t['date']:>10} {t['strike']:>7} {t['direction']:>4} {actual_pct:10.2f}% {'N/A':>9} "
              f"{'no data':>15} {t['exit_reason']:>14}")

print()
print(f"priced trades: {n_priced}/{len(trades)}")
print(f"total actual pnl (priced trades only): {total_actual:.2f}%  avg {total_actual/n_priced:.2f}%/trade" if n_priced else "")
print(f"total OPPOSITE-side pnl (same bars, flipped direction): {total_opposite:.2f}%  avg {total_opposite/n_priced:.2f}%/trade" if n_priced else "")
n_losses = sum(1 for t in trades if (t["pnl_pct"] or 0) < 0)
print(f"of {n_losses} losing trades, opposite side would have WON: {n_would_flip_win}")
