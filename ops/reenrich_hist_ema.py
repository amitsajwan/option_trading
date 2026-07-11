#!/usr/bin/env python3
"""One-off: enrich stored hist snapshots with the EMA/compression family and
max_pain/ATM-OI aggregates (2026-07-11 A4 findings — replay/study scores were
dampened vs live; direction lever blind). Runs enrich_day_ema_and_aggregates
(the same code future backfills use) over every stored day and writes the
payload back. Idempotent.

Run with the UPDATED repo mounted so the new enrich function is used:
  docker run --rm --name reenrich --network option_trading_default \
    -v /opt/option_trading:/repo -e PYTHONPATH=/repo -w /repo \
    option_trading-seller_app python /repo/ops/reenrich_hist_ema.py
"""
from pymongo import MongoClient, UpdateOne

from market_data_dashboard.services.hist_backfill import enrich_day_ema_and_aggregates

db = MongoClient("mongo", 27017).trading_ai

for coll_name in ("phase1_market_snapshots_hist", "phase1_market_snapshots_hist_nifty"):
    coll = db[coll_name]
    days = sorted(d for d in coll.distinct("trade_date_ist") if d)
    print(f"{coll_name}: {len(days)} days", flush=True)
    for di, day in enumerate(days):
        docs = list(coll.find({"trade_date_ist": day}).sort("timestamp", 1))
        snaps = [(d["_id"], (d.get("payload") or {}).get("snapshot")) for d in docs]
        payloads = [s for _, s in snaps if s]
        if not payloads:
            continue
        enrich_day_ema_and_aggregates(payloads)
        ops = [UpdateOne({"_id": _id}, {"$set": {"payload.snapshot": s}})
               for _id, s in snaps if s]
        coll.bulk_write(ops, ordered=False)
        if (di + 1) % 50 == 0:
            print(f"  ...{di+1}/{len(days)}", flush=True)
    print(f"{coll_name}: DONE", flush=True)
print("ALL DONE", flush=True)
