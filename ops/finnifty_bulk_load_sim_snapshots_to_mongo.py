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
from datetime import datetime, timezone

import pandas as pd
from pymongo import MongoClient

# Reuse the CANONICAL quality gate + collection-naming helpers, not a
# reimplementation -- build_training_view_from_mongo.py only trusts days
# with a passing dataset_manifests entry (2026-07-10 clean-rebuild mandate:
# "a day that fails does NOT enter Mongo"), and this loader must honor the
# exact same bar BankNifty/NIFTY historical data was held to.
from market_data_dashboard.services.hist_backfill import _coll_for, _quality_check


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots-dir", required=True)
    ap.add_argument("--instrument", default="FINNIFTY")
    ap.add_argument("--only-date", default=None, help="Load a single trade_date=YYYY-MM-DD partition only (smoke test)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    coll_name = _coll_for(args.instrument)
    db = MongoClient("mongo", 27017).trading_ai
    coll = db[coll_name]

    pattern = f"{args.snapshots_dir}/trade_date={args.only_date or '*'}/data.parquet"
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"No files matched: {pattern}", file=sys.stderr)
        return 1
    print(f"Found {len(files)} day-partition file(s), target collection={coll_name}")

    total_inserted = 0
    total_failed = 0
    for fpath in files:
        df = pd.read_parquet(fpath)
        snapshots = [
            json.loads(row["snapshot_raw_json"]) if isinstance(row["snapshot_raw_json"], str)
            else dict(row["snapshot_raw_json"])
            for _, row in df.iterrows()
        ]
        trade_date = snapshots[0].get("trade_date") if snapshots else None
        ok, report = _quality_check(snapshots)
        report.update({"instrument": args.instrument, "trade_date": trade_date,
                        "verified_at": datetime.now(timezone.utc).isoformat(), "passed": ok})

        if args.dry_run:
            print(f"[DRY RUN] {fpath}: quality={'PASS' if ok else 'FAIL:' + str(report.get('fail'))} n={len(snapshots)}")
            continue

        db["dataset_manifests"].update_one(
            {"_id": f"{coll_name}:{trade_date}"}, {"$set": report}, upsert=True)
        if not ok:
            total_failed += 1
            print(f"  {fpath}: QUALITY_FAIL {report.get('fail')} -- day NOT ingested")
            continue

        docs = [{
            "event_type": "hist_backfill",
            "snapshot_id": s.get("snapshot_id"),
            "instrument": args.instrument,
            "timestamp": s.get("timestamp"),
            "trade_date_ist": trade_date,
            "payload": {"snapshot": s},
        } for s in snapshots]
        coll.delete_many({"trade_date_ist": trade_date})  # idempotent per day, same as hist_backfill.backfill_day
        result = coll.insert_many(docs)
        total_inserted += len(result.inserted_ids)
        print(f"  {fpath}: inserted {len(result.inserted_ids)} docs")

    print(f"TOTAL inserted: {total_inserted}  failed_quality_gate: {total_failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
