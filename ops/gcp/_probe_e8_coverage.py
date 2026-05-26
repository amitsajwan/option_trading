"""Check E8 replay coverage — did July run? Where did decision traces fall?"""
from pymongo import MongoClient
from collections import Counter
import sys

rid = sys.argv[1] if len(sys.argv) > 1 else "281b3ad4-fd6d-4157-bdac-08bec8822771"
db = MongoClient("mongodb://mongo:27017").trading_ai

opens = Counter()
for d in db.strategy_positions_historical.find({"run_id": rid, "event": "POSITION_OPEN"}, {"trade_date_ist": 1}):
    td = str(d.get("trade_date_ist") or "")
    if td:
        opens[td[:7]] += 1
print("POSITION_OPEN by month:", dict(opens))

closes = Counter()
for d in db.strategy_positions_historical.find({"run_id": rid, "event": "POSITION_CLOSE"}, {"trade_date_ist": 1}):
    td = str(d.get("trade_date_ist") or "")
    if td:
        closes[td[:7]] += 1
print("POSITION_CLOSE by month:", dict(closes))

# Decision traces show whether engine SAW July snapshots at all
tcoll = db.strategy_decision_traces_historical
total_traces = tcoll.count_documents({"run_id": rid})
print(f"total decision_traces: {total_traces}")
trace_months = Counter()
for d in tcoll.find({"run_id": rid}, {"trade_date_ist": 1}).limit(20000):
    td = str(d.get("trade_date_ist") or "")
    if td:
        trace_months[td[:7]] += 1
print(f"decision_traces sampled by month: {dict(trace_months)}")

# Engine-side regime tag logs would help but may have been rotated.
# Check what dates DID enter the engine at all
dates_seen = sorted({d.get("trade_date_ist") for d in tcoll.find(
    {"run_id": rid}, {"trade_date_ist": 1}
).limit(10000)})
print(f"distinct trade_dates in decision_traces sample: {len(dates_seen)}")
if dates_seen:
    print(f"first/last in sample: {dates_seen[0]} / {dates_seen[-1]}")
