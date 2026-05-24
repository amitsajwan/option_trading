#!/usr/bin/env python3
import json
import sys
import urllib.request
from pymongo import MongoClient

ids = sys.argv[1:] or [
    "202d3efd-b48f-45dd-ba22-d4767a1fa7e8",
    "f088cdd8-be12-4a64-b2cb-c8be6d6361b9",
    "7c42cd7c-8cec-4ee9-9d92-ded8d9c96359",
    "57e60de8-0cde-4117-a4a8-da1a6fe3b79d",
]
db = MongoClient("mongodb://mongo:27017")["trading_ai"]
for rid in ids:
    with urllib.request.urlopen(
        f"http://127.0.0.1:8008/api/strategy/evaluation/runs/{rid}", timeout=15
    ) as r:
        d = json.load(r)
    n = db.strategy_positions_historical.count_documents(
        {"run_id": rid, "event": "POSITION_CLOSE"}
    )
    print(
        rid[:8],
        d.get("status"),
        d.get("date_from"),
        d.get("date_to"),
        (d.get("message") or "")[:60],
        f"trades={n}",
    )
