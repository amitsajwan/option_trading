import sys
from pymongo import MongoClient

try:
    client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
    db = client["trading_ai"]
    dates = sorted(db["phase1_market_snapshots"].distinct(
        "trade_date_ist",
        {"trade_date_ist": {"$gte": "2026-06-01", "$lte": "2026-06-30"}}
    ))
    print("JUNE_2026_DATES=" + ",".join(dates))
    for d in dates:
        cnt = db["phase1_market_snapshots"].count_documents({"trade_date_ist": d})
        print(f"  {d}: {cnt} snapshots")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
