"""Find what daily BANKNIFTY OHLC data is available for regime tagging."""
from pymongo import MongoClient
from collections import Counter
import json

db = MongoClient("mongodb://mongo:27017").trading_ai

# 1. List all collections that might carry daily/index data
print("## Candidate collections (name contains nifty/index/daily/ohlc/futures)")
for c in sorted(db.list_collection_names()):
    if any(k in c.lower() for k in ["nifty", "index", "daily", "ohlc", "futures", "underlying", "bn"]):
        try:
            n = db[c].estimated_document_count()
        except Exception:
            n = "?"
        print(f"  {c:<55} n~{n}")

# 2. The snapshots already used for replay carry futures price per minute — we can build daily from them
print("\n## phase1_market_snapshots_historical sample (already used for replay):")
doc = db.phase1_market_snapshots_historical.find_one()
if doc:
    interesting = {k: doc.get(k) for k in [
        "trade_date_ist", "market_time_ist", "timestamp",
        "futures_close", "fut_close", "underlying_close", "spot_close",
        "open", "high", "low", "close", "atm_strike",
    ] if doc.get(k) is not None}
    print("  fields seen:", sorted(doc.keys())[:40])
    print("  interesting:", json.dumps(interesting, default=str, indent=2))

# 3. Or pull from POSITION_OPEN docs which carry entry_futures_price
print("\n## Distinct trade dates in Ref run (sanity)")
dates = sorted(set(
    str(d.get("trade_date_ist") or "")
    for d in db.strategy_positions_historical.find(
        {"run_id": "ae5a86b7-9198-4e64-9399-fd5fea03e293", "event": "POSITION_OPEN"},
        {"trade_date_ist": 1}
    )
))
print(f"  n_dates={len(dates)}  first={dates[0] if dates else None}  last={dates[-1] if dates else None}")

# 4. Daily-OHLC derivable from per-bar snapshots — first/last/min/max futures_close per day
print("\n## Try to derive daily OHLC from phase1 snapshots for one day")
sample_day = dates[10] if len(dates) > 10 else None
if sample_day:
    rows = list(db.phase1_market_snapshots_historical.find(
        {"trade_date_ist": sample_day},
        {"market_time_ist": 1, "fut_close": 1, "futures_close": 1, "futures_derived": 1, "timestamp": 1}
    ).sort("timestamp", 1).limit(5))
    print(f"  sample_day={sample_day}  first 5 rows:")
    for r in rows:
        r.pop("_id", None)
        print(f"    {r}")
