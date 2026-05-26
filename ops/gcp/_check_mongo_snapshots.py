"""Check if MongoDB has historical IS snapshot data."""
from pymongo import MongoClient
import sys

c = MongoClient("mongodb://localhost:27017")
db = c["trading_ai"]
colls = db.list_collection_names()
print("collections:", colls)

for coll_name in ["phase1_market_snapshots", "snapshots", "market_snapshots"]:
    if coll_name in colls:
        coll = db[coll_name]
        count = coll.count_documents({})
        print(f"{coll_name}: {count} docs")
        if count > 0:
            sample = coll.find_one({}, {"_id": 0, "trade_date": 1, "timestamp": 1})
            print(f"  sample: {sample}")
            oldest = coll.find_one({}, {"_id": 0, "trade_date": 1}, sort=[("trade_date", 1)])
            newest = coll.find_one({}, {"_id": 0, "trade_date": 1}, sort=[("trade_date", -1)])
            print(f"  range: {oldest} -> {newest}")
