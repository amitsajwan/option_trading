#!/usr/bin/env python3
import sys
from collections import Counter
from pymongo import MongoClient

rid = sys.argv[1]
db = MongoClient("mongodb://mongo:27017")["trading_ai"]
c = Counter()
for d in db.strategy_positions_historical.find(
    {"run_id": rid, "event": "POSITION_CLOSE"}, {"trade_date_ist": 1}
):
    c[str(d.get("trade_date_ist") or "?")[:10]] += 1
print("run", rid[:8], "total", sum(c.values()))
for day, n in sorted(c.items()):
    print(day, n)
