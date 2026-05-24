#!/usr/bin/env python3
from pymongo import MongoClient

db = MongoClient("mongodb://mongo:27017")["trading_ai"]
for doc in db.strategy_eval_runs.find(
    {"date_from": {"$gte": "2024-08-01"}, "dataset": "historical"},
    {"run_id": 1, "date_from": 1, "date_to": 1, "status": 1, "message": 1, "submitted_at": 1},
).sort("submitted_at", -1).limit(20):
    rid = doc["run_id"]
    n = db.strategy_positions_historical.count_documents(
        {"run_id": rid, "event": "POSITION_CLOSE"}
    )
    print(rid, n, doc.get("date_from"), doc.get("date_to"), doc.get("status"), doc.get("submitted_at", "")[:19])
