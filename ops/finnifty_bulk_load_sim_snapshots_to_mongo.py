#!/usr/bin/env python3
"""Bulk-load FINNIFTY sim-snapshot parquet (dhan_build_sim_snapshots.py output)
directly into Mongo's phase1_market_snapshots_hist_finnifty collection.

Why a direct loader instead of replaying through Redis/strategy_app/
persistence_app (the mechanism used for BankNifty's OOS testing all
session): this is a one-time historical BULK load, not a live-fidelity
replay -- the sim-snapshot rows are already fully-formed, live-shaped
snapshot dicts (confirmed 2026-07-26: same schema as payload.snapshot in
existing phase1_market_snapshots_hist documents -- schema_name,
snapshot_id, instrument, session_context, futures_bar, strikes, etc.).
Wrapping each row in the same document envelope and inserting directly
is simpler and far more reliable than standing up a temporary live
pipeline just to re-derive the same data.

Usage (inside a container with pymongo + pandas, on the compose network):
  docker run --rm --network option_trading_default \
    -v /path/finnifty_bulk_load_sim_snapshots_to_mongo.py:/load.py \
    -v /opt/option_trading/.data_finnifty:/data \
    option_trading-dashboard python /load.py \
    --snapshots-dir /data/dhan_pipeline_sim_finnifty/snapshots \
    --instrument FINNIFTY
"""
from __future__ import annotations

import argparse
import glob
import json
import sys

import pandas as pd
from pymongo import MongoClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots-dir", required=True)
    ap.add_argument("--instrument", default="FINNIFTY")
    ap.add_argument("--only-date", default=None, help="Load a single trade_date=YYYY-MM-DD partition only (smoke test)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    coll_name = f"phase1_market_snapshots_hist_{args.instrument.lower()}"
    db = MongoClient("mongo", 27017).trading_ai
    coll = db[coll_name]

    pattern = f"{args.snapshots_dir}/trade_date={args.only_date or '*'}/data.parquet"
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"No files matched: {pattern}", file=sys.stderr)
        return 1
    print(f"Found {len(files)} day-partition file(s), target collection={coll_name}")

    total_inserted = 0
    for fpath in files:
        df = pd.read_parquet(fpath)
        docs = []
        for _, row in df.iterrows():
            snap = json.loads(row["snapshot_raw_json"]) if isinstance(row["snapshot_raw_json"], str) else dict(row["snapshot_raw_json"])
            snapshot_id = snap.get("snapshot_id") or row.get("snapshot_id")
            ts = snap.get("timestamp") or row.get("timestamp")
            trade_date = snap.get("trade_date") or row.get("trade_date")
            docs.append({
                "event_type": "hist_backfill",
                "snapshot_id": snapshot_id,
                "instrument": args.instrument,
                "timestamp": ts,
                "trade_date_ist": trade_date,
                "payload": {"snapshot": snap},
            })
        if args.dry_run:
            print(f"[DRY RUN] {fpath}: would insert {len(docs)} docs (sample snapshot_id={docs[0]['snapshot_id'] if docs else None})")
            continue
        if docs:
            # Idempotent: drop any prior docs for these exact snapshot_ids first,
            # so re-running this loader (e.g. after a fix) doesn't duplicate.
            ids = [d["snapshot_id"] for d in docs]
            coll.delete_many({"instrument": args.instrument, "snapshot_id": {"$in": ids}})
            result = coll.insert_many(docs)
            total_inserted += len(result.inserted_ids)
            print(f"  {fpath}: inserted {len(result.inserted_ids)} docs")

    print(f"TOTAL inserted: {total_inserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
