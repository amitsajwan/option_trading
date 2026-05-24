#!/usr/bin/env python3
"""Find historical eval runs with trade counts by date window."""
import json
import urllib.request
from pymongo import MongoClient

db = MongoClient("mongodb://mongo:27017")["trading_ai"]
with urllib.request.urlopen(
    "http://127.0.0.1:8008/api/strategy/evaluation/runs?dataset=historical&limit=30",
    timeout=15,
) as r:
    runs = json.load(r)
if isinstance(runs, dict):
    runs = runs.get("runs") or runs.get("items") or []
for run in runs:
    rid = str(run.get("run_id") or "")
    if not rid:
        continue
    n = db.strategy_positions_historical.count_documents(
        {"run_id": rid, "event": "POSITION_CLOSE"}
    )
    if n < 5:
        continue
    print(
        rid[:8],
        n,
        run.get("date_from"),
        run.get("date_to"),
        (run.get("message") or "")[:40],
        run.get("submitted_at", "")[:10],
    )
