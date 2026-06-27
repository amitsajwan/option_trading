import json, sys
from pymongo import MongoClient

def check_run(client, run_id, date):
    db = client["trading_ai"]
    prefixes = [f"strategy_signals_sim_{run_id}", f"strategy_positions_sim_{run_id}"]
    
    signal_count = 0
    pos_count = 0
    for coll_name in db.list_collection_names():
        if run_id.replace('-', '') in coll_name.lower() or run_id in coll_name:
            if 'signal' in coll_name.lower():
                signal_count = db[coll_name].count_documents({})
            elif 'position' in coll_name.lower():
                pos_count = db[coll_name].count_documents({})
    
    # Also check run_dir result.json
    result_path = f"/opt/option_trading/.run/strategy_app_sim/{run_id}/result.json"
    try:
        with open(result_path) as f:
            result = json.load(f)
        total = result.get("total_published", "?")
    except Exception:
        total = "?"
    
    return signal_count, pos_count, total

client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=5000)

RUNS = [
    ("a7729382-daf9-4666-94a0-fd7e94897618", "2026-06-01"),
    ("90398e41-c2a9-4838-b274-b68cafa2fe77", "2026-06-02"),
    ("c323b149-30b9-416e-8436-1bdcad2c5d4f", "2026-06-03"),
    ("32fca5d4-d648-4848-a74d-9b9f0c44a792", "2026-06-10"),
    ("05cd2f2f-f8e6-4ead-9163-f778240a5d43", "2026-06-11"),
    ("de13bfcd-6bfa-4878-8488-a69b42213cc2", "2026-06-12"),
    ("a49914b2-bfe9-48bd-bb25-9089bfe8212b", "2026-06-15"),
    ("ba77702e-cfc0-4e5c-b2e-61aa0ebae94e", "2026-06-16"),
    ("8546e2e9-0238-466a-bc52-35de297d4e0f", "2026-06-17"),
]

print("Run ID         Date       Signals  Positions  Snapshots")
print("-" * 55)
for rid, date in RUNS:
    sig, pos, total = check_run(client, rid, date)
    print(f"{rid[:8]}  {date}  {sig:>7}  {pos:>9}  {total}")
