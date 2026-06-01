#!/usr/bin/env python3
"""Promote one trading day's live snapshots into the historical collection.

Idempotent — uses upsert keyed on `_id`. Safe to re-run; safe to run multiple
days back-to-back.

Usage:
    python3 promote_today_to_historical.py            # today (IST)
    python3 promote_today_to_historical.py 2026-05-27 # specific date

Designed to run on the GCP VM (inside or via docker exec on the mongo
container). Reads MONGO_URI / MONGO_DB from env, falling back to the
docker-compose live defaults.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

try:
    from pymongo import MongoClient, UpdateOne
except ImportError:
    sys.stderr.write("pymongo not installed; run `pip install pymongo`\n")
    sys.exit(2)

_IST = timezone(timedelta(hours=5, minutes=30))

LIVE_COLL = os.getenv("MONGO_COLL_SNAPSHOTS", "phase1_market_snapshots")
HIST_COLL = os.getenv(
    "MONGO_COLL_SNAPSHOTS_HISTORICAL", "phase1_market_snapshots_historical"
)
MONGO_URI = os.getenv("MONGO_URI") or (
    f"mongodb://{os.getenv('MONGO_HOST', 'localhost')}:{os.getenv('MONGO_PORT', '27017')}"
)
MONGO_DB = os.getenv("MONGO_DB", "trading_ai")

BATCH = 500


def resolve_date(argv: list[str]) -> str:
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    return datetime.now(tz=_IST).date().isoformat()


def main() -> int:
    date_str = resolve_date(sys.argv)
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    src = db[LIVE_COLL]
    dst = db[HIST_COLL]

    total = src.count_documents({"trade_date_ist": date_str})
    if total == 0:
        sys.stderr.write(
            f"no live snapshots found for trade_date_ist={date_str} in {LIVE_COLL}\n"
        )
        return 1

    print(f"promoting trade_date_ist={date_str}: {total} live snapshots -> {HIST_COLL}")
    cursor = src.find({"trade_date_ist": date_str}).sort("_id", 1)

    ops: list[UpdateOne] = []
    written = 0
    for doc in cursor:
        _id = doc["_id"]
        ops.append(UpdateOne({"_id": _id}, {"$set": doc}, upsert=True))
        if len(ops) >= BATCH:
            r = dst.bulk_write(ops, ordered=False)
            written += (r.upserted_count or 0) + (r.modified_count or 0)
            print(f"  flushed batch upserted={r.upserted_count} modified={r.modified_count} so_far={written}")
            ops = []
    if ops:
        r = dst.bulk_write(ops, ordered=False)
        written += (r.upserted_count or 0) + (r.modified_count or 0)
        print(f"  flushed final upserted={r.upserted_count} modified={r.modified_count} so_far={written}")

    final = dst.count_documents({"trade_date_ist": date_str})
    print(f"done. dst now has {final} docs for trade_date_ist={date_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
