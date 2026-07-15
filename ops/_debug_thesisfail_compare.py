#!/usr/bin/env python3
"""One-off: dump deduplicated closed trades for a run_id pattern after a
given timestamp cutoff, for comparing the thesis-fail-bars-raised test
against the baseline. Not part of the permanent pipeline."""
import sys
from pymongo import MongoClient

cutoff = sys.argv[1] if len(sys.argv) > 1 else "2026-07-15T12:02:33"
run_pattern = sys.argv[2] if len(sys.argv) > 2 else "replay-multiday-2026-06-0[1-4]-banknifty"

db = MongoClient("mongo", 27017).trading_ai
closes = list(db.strategy_positions_historical.find({
    "exit_reason": {"$ne": None},
    "run_id": {"$regex": f"^{run_pattern}$"},
    "received_at_ist": {"$gte": cutoff},
}).sort("timestamp", 1))

seen = set()
trades = []
for p in closes:
    pos = (p.get("payload") or {}).get("position") or {}
    # 2026-07-15: position_id alone under-deduplicates — known duplicate-write
    # issue (flagged, root cause not chased). Dedup on the full semantic
    # identity of the closed trade instead: two records with the same date,
    # strike, direction, bars_held, pnl, and exit_reason are the same logical
    # trade written twice, not two coincidentally-identical real trades.
    key = (p.get("trade_date_ist"), p.get("strike"), p.get("direction"),
           pos.get("bars_held"), round((p.get("pnl_pct") or 0), 6), p.get("exit_reason"))
    if key in seen:
        continue
    seen.add(key)
    trades.append({
        "date": p.get("trade_date_ist"), "strike": p.get("strike"), "dir": p.get("direction"),
        "bars": pos.get("bars_held"), "pnl": (p.get("pnl_pct") or 0) * 100,
        "mfe": (pos.get("mfe_pct") or 0) * 100, "reason": p.get("exit_reason"),
    })

print(f"n distinct trades: {len(trades)}\n")
total = 0.0
wins = 0
for t in trades:
    total += t["pnl"]
    if t["pnl"] > 0:
        wins += 1
    print(f"{t['date']} {t['strike']:>6} {t['dir']:>3} bars={t['bars']:>3} "
          f"pnl={t['pnl']:7.2f}% mfe={t['mfe']:6.2f}% {t['reason']}")

print()
if trades:
    print(f"TOTAL: {total:.2f}%  avg={total/len(trades):.2f}%/trade  "
          f"win_rate={100*wins/len(trades):.1f}%  n={len(trades)}")
