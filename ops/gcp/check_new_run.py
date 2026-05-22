from pymongo import MongoClient
db = MongoClient('mongodb://mongo:27017').trading_ai

# Check runs from today by received_at, look for DET_DIRECTION stops
pipeline = [
    {"$match": {"event": "POSITION_CLOSE", "received_at_ist": {"$gte": "2026-05-22"}}},
    {"$group": {
        "_id": "$run_id",
        "trades": {"$sum": 1},
        "first": {"$min": "$trade_date_ist"},
        "last": {"$max": "$trade_date_ist"},
        "received": {"$max": "$received_at_ist"},
    }},
    {"$sort": {"received": -1}},
]
print("All runs received today:")
runs = list(db.strategy_positions_historical.aggregate(pipeline))
for r in runs:
    print(f"  {r['_id']}  n={r['trades']}  {r['first']}..{r['last']}  recv={r['received'][:19]}")

# For each run with >10 trades from Aug-Oct, check DET_DIRECTION stop
print()
for r in runs:
    if r['trades'] >= 10:
        run_id = r['_id']
        det = list(db.strategy_positions_historical.find(
            {"run_id": run_id, "event": "POSITION_CLOSE", "entry_strategy": "DET_DIRECTION"},
            {"trade_date_ist": 1, "stop_loss_pct": 1, "target_pct": 1, "exit_reason": 1, "pnl_pct": 1, "_id": 0}
        ).limit(3))
        stops = set(round(d.get("stop_loss_pct", 0) * 100) for d in det)
        print(f"  run={run_id[:8]}  det_dir_trades={len(list(db.strategy_positions_historical.find({'run_id': run_id, 'event': 'POSITION_CLOSE', 'entry_strategy': 'DET_DIRECTION'})))}  stops={stops}")
        for d in det:
            print(f"    {d}")
