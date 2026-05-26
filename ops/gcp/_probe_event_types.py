"""Probe what per-trade event/update data is available for counterfactual sim."""
from pymongo import MongoClient
import sys, json

run_id = sys.argv[1] if len(sys.argv) > 1 else "ae5a86b7-9198-4e64-9399-fd5fea03e293"
db = MongoClient("mongodb://mongo:27017").trading_ai

print(f"# Probe for {run_id}\n")
pipeline = [
    {"$match": {"run_id": run_id}},
    {"$group": {"_id": "$event", "n": {"$sum": 1}}},
    {"$sort": {"n": -1}},
]
print("## Events in strategy_positions_historical")
for r in db.strategy_positions_historical.aggregate(pipeline):
    print(f"  {r['_id']:<25} {r['n']}")

print("\n## Events in trade_signals_historical")
for r in db.trade_signals_historical.aggregate(pipeline):
    print(f"  {r['_id']:<25} {r['n']}")

print("\n## Sample non-CLOSE doc from strategy_positions_historical (first 30 keys):")
non_close = db.strategy_positions_historical.find_one(
    {"run_id": run_id, "event": {"$ne": "POSITION_CLOSE"}}
)
if non_close:
    keys = sorted(non_close.keys())
    print(f"  event={non_close.get('event')!r}  keys={keys}")
    interesting = {k: non_close[k] for k in keys if k in (
        "event","timestamp","market_time_ist","position_id","direction",
        "entry_premium","current_premium","pnl_pct","mfe_pct","mae_pct",
        "bars_held","high_water_premium"
    )}
    print("  interesting:", json.dumps(interesting, default=str, indent=2))

print("\n## Per-position bar counts (n updates per position_id, first 5):")
agg = list(db.strategy_positions_historical.aggregate([
    {"$match": {"run_id": run_id}},
    {"$group": {"_id": "$position_id", "events": {"$sum": 1}}},
    {"$sort": {"events": -1}},
    {"$limit": 5},
]))
for r in agg:
    print(f"  position_id={r['_id']}  events={r['events']}")
