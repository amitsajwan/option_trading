#!/usr/bin/env python3
"""For each closed trade, find the BEST premium the actually-bought option
(CE or PE, matching the trade's own direction) reached at ANY point within
the model's own 15-minute prediction window from entry — regardless of when
the real exit fired. This is the theoretical upper bound: if literally the
best possible exit within the window had been taken, what would the P&L
have been? Removes exit-policy timing from the question entirely.
(2026-07-15, user request.)

Usage: python ops/study_best_possible_exit.py "replay-multiday-2026-06-0[1-4]-banknifty" BANKNIFTY [gte_cutoff_ts] [lt_cutoff_ts]
"""
import sys
from datetime import datetime, timedelta
from pymongo import MongoClient

run_pattern = sys.argv[1] if len(sys.argv) > 1 else "replay-multiday-2026-06-0[1-4]-banknifty"
instrument = sys.argv[2].upper() if len(sys.argv) > 2 else "BANKNIFTY"
gte_cutoff = sys.argv[3] if len(sys.argv) > 3 else None
lt_cutoff = sys.argv[4] if len(sys.argv) > 4 else None
COLL = "phase1_market_snapshots_hist" if instrument == "BANKNIFTY" else "phase1_market_snapshots_hist_nifty"

db = MongoClient("mongo", 27017).trading_ai
pos_coll = db.strategy_positions_historical
snap_coll = db[COLL]

query = {"exit_reason": {"$ne": None}, "run_id": {"$regex": f"^{run_pattern}$"}}
if gte_cutoff or lt_cutoff:
    query["received_at_ist"] = {}
    if gte_cutoff:
        query["received_at_ist"]["$gte"] = gte_cutoff
    if lt_cutoff:
        query["received_at_ist"]["$lt"] = lt_cutoff
closes = list(pos_coll.find(query).sort("timestamp", 1))

seen = set()
trades = []
for p in closes:
    pos = (p.get("payload") or {}).get("position") or {}
    key = (p.get("trade_date_ist"), p.get("strike"), p.get("direction"),
           pos.get("bars_held"), p.get("pnl_pct"), p.get("exit_reason"))
    if key in seen:
        continue
    seen.add(key)
    trades.append({
        "date": p.get("trade_date_ist"), "strike": p.get("strike"), "dir": p.get("direction"),
        "entry_time": pos.get("entry_time"), "entry_premium": pos.get("entry_premium"),
        "actual_pnl": (p.get("pnl_pct") or 0) * 100, "bars_held": pos.get("bars_held"),
        "exit_reason": p.get("exit_reason"),
    })

print(f"{len(trades)} distinct trades\n")
print(f"{'date':>10} {'strike':>7} {'dir':>3} {'actual':>8} {'best_in_15m':>12} {'best_bar':>9} {'left_on_table':>14}")

total_actual = 0.0
total_best = 0.0
n_much_better = 0
for t in trades:
    if not t["entry_time"] or t["entry_premium"] is None:
        continue
    entry_dt = datetime.fromisoformat(t["entry_time"])
    window_end = entry_dt + timedelta(minutes=15)
    side = "ce_ltp" if t["dir"] == "CE" else "pe_ltp"

    docs = list(snap_coll.find(
        {"trade_date_ist": t["date"]},
        {"timestamp": 1, "payload.snapshot.strikes": 1, "snapshot_id": 1},
    ).sort("timestamp", 1))

    best_premium = None
    best_ts = None
    for doc in docs:
        ts_raw = doc.get("timestamp") or (doc.get("payload") or {}).get("snapshot", {}).get("timestamp")
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except Exception:
            continue
        if ts < entry_dt or ts > window_end:
            continue
        snap = (doc.get("payload") or {}).get("snapshot") or {}
        for row in (snap.get("strikes") or []):
            if row.get("strike") == t["strike"]:
                val = row.get(side)
                if val is not None and (best_premium is None or val > best_premium):
                    best_premium = val
                    best_ts = ts
                break

    if best_premium is None or t["entry_premium"] in (None, 0):
        print(f"{t['date']:>10} {t['strike']:>7} {t['dir']:>3} {t['actual_pnl']:7.2f}% {'NO DATA':>12}")
        continue

    best_pnl = 100.0 * (best_premium - t["entry_premium"]) / t["entry_premium"]
    best_bar_min = int((best_ts - entry_dt).total_seconds() / 60) if best_ts else None
    left_on_table = best_pnl - t["actual_pnl"]
    total_actual += t["actual_pnl"]
    total_best += best_pnl
    if left_on_table > 3.0:
        n_much_better += 1
    print(f"{t['date']:>10} {t['strike']:>7} {t['dir']:>3} {t['actual_pnl']:7.2f}% {best_pnl:11.2f}% "
          f"{best_bar_min if best_bar_min is not None else '?':>7}min {left_on_table:13.2f}%")

n = len(trades)
print()
print(f"TOTAL actual: {total_actual:.2f}%  avg={total_actual/n:.2f}%/trade")
print(f"TOTAL best-possible-within-15min: {total_best:.2f}%  avg={total_best/n:.2f}%/trade")
print(f"trades where best-possible was >3pp better than actual: {n_much_better}/{n}")
