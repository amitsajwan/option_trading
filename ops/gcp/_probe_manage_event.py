"""Inspect a POSITION_MANAGE doc."""
from pymongo import MongoClient
import sys, json

run_id = sys.argv[1] if len(sys.argv) > 1 else "ae5a86b7-9198-4e64-9399-fd5fea03e293"
db = MongoClient("mongodb://mongo:27017").trading_ai

# Pick a position_id with several manage events
pid_row = list(db.strategy_positions_historical.aggregate([
    {"$match": {"run_id": run_id, "event": "POSITION_MANAGE"}},
    {"$group": {"_id": "$position_id", "n": {"$sum": 1}}},
    {"$sort": {"n": -1}},
    {"$limit": 1},
]))
pid = pid_row[0]["_id"]
print(f"Sample position_id={pid} (with {pid_row[0]['n']} manage events)\n")

docs = list(db.strategy_positions_historical.find(
    {"run_id": run_id, "position_id": pid}
).sort("timestamp", 1))

print(f"Total events for this position: {len(docs)}")
for i, d in enumerate(docs[:5]):
    print(f"\n--- event #{i}  type={d.get('event')} ---")
    sub = {k: d.get(k) for k in [
        "event","timestamp","market_time_ist","bars_held","current_premium",
        "entry_premium","pnl_pct","mfe_pct","mae_pct","exit_reason","direction"
    ] if d.get(k) is not None}
    print(json.dumps(sub, default=str, indent=2))
