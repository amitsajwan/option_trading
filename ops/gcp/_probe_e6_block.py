"""Why did E6_ce_only produce 540 trades instead of CE-only ~240?
Check: direction distribution, direction_source on close docs, and a sample vote payload."""
from pymongo import MongoClient
import sys, json
from collections import Counter

run_id = sys.argv[1] if len(sys.argv) > 1 else "7d452a15-4b77-4687-b05c-169a15b805de"
db = MongoClient("mongodb://mongo:27017").trading_ai

dirs = Counter()
sources = Counter()
strats = Counter()
profile_ids = Counter()
for d in db.strategy_positions_historical.find(
    {"run_id": run_id, "event": "POSITION_CLOSE"},
    {"direction": 1, "entry_strategy": 1, "strategy_profile_id": 1, "reason": 1, "payload": 1}
):
    dirs[d.get("direction")] += 1
    strats[d.get("entry_strategy")] += 1
    profile_ids[d.get("strategy_profile_id")] += 1
    p = d.get("payload") or {}
    if isinstance(p, dict):
        sources[(p.get("raw_signals") or {}).get("direction_source", "unknown") if isinstance(p.get("raw_signals"), dict) else "no_raw"] += 1

print(f"E6 run_id: {run_id}")
print(f"closes:    {sum(dirs.values())}")
print(f"directions: {dict(dirs)}")
print(f"entry_strategy: {dict(strats)}")
print(f"strategy_profile_id: {dict(profile_ids)}")
print(f"direction_source on payload.raw_signals: {dict(sources)}")

# Sample close doc to see structure
print("\nSample close doc keys:")
d = db.strategy_positions_historical.find_one({"run_id": run_id, "event": "POSITION_CLOSE"})
if d:
    payload = d.get("payload") or {}
    print(json.dumps({"top_keys": sorted(d.keys()), "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else "n/a"}, indent=2))

# Check what env was active by sampling one ML_ENTRY vote
print("\nSample ML_ENTRY vote raw_signals:")
v = db.strategy_votes_historical.find_one({"run_id": run_id, "strategy": "ML_ENTRY"})
if v:
    rs = v.get("raw_signals") or {}
    print(json.dumps({k: rs.get(k) for k in ["direction_source", "ml_direction_hint", "ml_direction_hint_source", "_ml_entry_timing_only"]}, indent=2, default=str))
else:
    print("  no votes found")
