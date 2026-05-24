#!/usr/bin/env python3
from pymongo import MongoClient

db = MongoClient("mongodb://mongo:27017")["trading_ai"]
pipe = [
    {"$match": {"event": "POSITION_CLOSE"}},
    {"$group": {"_id": "$run_id", "n": {"$sum": 1}}},
    {"$sort": {"n": -1}},
    {"$limit": 25},
]
for row in db.strategy_positions_historical.aggregate(pipe):
    rid = row["_id"]
    n = row["n"]
    meta = db.strategy_eval_runs.find_one({"run_id": rid}) or {}
    print(
        str(rid)[:8],
        n,
        meta.get("date_from"),
        meta.get("date_to"),
        meta.get("status"),
        (meta.get("message") or "")[:35],
    )
