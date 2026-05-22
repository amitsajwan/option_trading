from pymongo import MongoClient
from collections import Counter
db = MongoClient('mongodb://mongo:27017').trading_ai

# Most recent run IDs by POSITION_CLOSE event
pipeline = [
    {"$match": {"event": "POSITION_CLOSE"}},
    {"$group": {"_id": "$run_id", "count": {"$sum": 1}, "last": {"$max": "$trade_date_ist"}, "first": {"$min": "$trade_date_ist"}}},
    {"$sort": {"last": -1}},
    {"$limit": 5},
]
print("Last 5 runs with POSITION_CLOSE events:")
for r in db.strategy_positions_historical.aggregate(pipeline):
    print(f"  run_id={r['_id']}  trades={r['count']}  {r['first']} .. {r['last']}")

# Also check the newest run's stop_loss_pct
print()
print("Newest run DET_DIRECTION stop check:")
pipeline2 = [
    {"$match": {"event": "POSITION_CLOSE"}},
    {"$group": {"_id": "$run_id", "last": {"$max": "$received_at_ist"}}},
    {"$sort": {"last": -1}},
    {"$limit": 1},
]
latest = list(db.strategy_positions_historical.aggregate(pipeline2))
if latest:
    run_id = latest[0]["_id"]
    print(f"  run_id = {run_id}")
    for doc in db.strategy_positions_historical.find(
        {"run_id": run_id, "event": "POSITION_CLOSE", "entry_strategy": "DET_DIRECTION"},
        {"trade_date_ist": 1, "stop_loss_pct": 1, "target_pct": 1, "direction": 1, "exit_reason": 1, "pnl_pct": 1, "_id": 0}
    ).limit(5):
        print(f"  {doc}")
