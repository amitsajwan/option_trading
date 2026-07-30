"""Export phase1_market_snapshots_hist{,_nifty} -> local canonical parquet.

The strategy_eval_orchestrator's replay path reads ONLY local parquet
(snapshot_app.historical.parquet_store.ParquetStore), never Mongo directly --
snapshots/trade_date=<date>/data.parquet with a snapshot_raw_json column
(schema confirmed against ml_pipeline_2/scripts/dhan_build_sim_snapshots.py's
real output, 2026-07-30 FINNIFTY work). This never existed for BankNifty/NIFTY
on this VM (parquet_data/snapshots was empty) even though the real, canonical,
quality-gated historical data has lived in Mongo since the 19-month baseline
work. This script bridges that gap: read already-quality-gated days straight
from Mongo (dataset_manifests.passed=True), write them out in the format the
replay/backtest tooling actually expects.

Usage:
    python3 ops/export_mongo_snapshots_to_parquet.py \
        --instrument BANKNIFTY --from 2026-04-01 --to 2026-07-17 \
        --out-base /app/.data/ml_pipeline/parquet_data
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from pymongo import MongoClient


def _coll_for(instrument: str) -> str:
    inst = instrument.strip().upper()
    if inst == "BANKNIFTY":
        return "phase1_market_snapshots_hist"
    return f"phase1_market_snapshots_hist_{inst.lower()}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", required=True)
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--out-base", default="/app/.data/ml_pipeline/parquet_data")
    ap.add_argument("--only-quality-passed", action="store_true", default=True)
    args = ap.parse_args()

    db = MongoClient("mongo", 27017).trading_ai
    coll = db[_coll_for(args.instrument)]
    manifests = db["dataset_manifests"]

    d = date.fromisoformat(args.date_from)
    end = date.fromisoformat(args.date_to)
    out_root = Path(args.out_base) / "snapshots"
    total_days = 0
    total_rows = 0
    skipped_quality = 0
    skipped_nodata = 0

    while d <= end:
        td = d.isoformat()
        if d.weekday() < 5:
            manifest = manifests.find_one({"_id": f"{coll.name}:{td}"})
            if args.only_quality_passed and (not manifest or not manifest.get("passed")):
                skipped_quality += 1
                d += timedelta(days=1)
                continue
            docs = list(
                coll.find({"trade_date_ist": td}, {"payload.snapshot": 1, "_id": 0}).sort("timestamp", 1)
            )
            if not docs:
                skipped_nodata += 1
                d += timedelta(days=1)
                continue
            rows = []
            for doc in docs:
                snap = (doc.get("payload") or {}).get("snapshot")
                if not snap:
                    continue
                rows.append({
                    "trade_date": snap.get("trade_date") or td,
                    "timestamp": snap.get("timestamp"),
                    "snapshot_id": snap.get("snapshot_id"),
                    "snapshot_raw_json": json.dumps(snap, default=str),
                })
            if rows:
                day_dir = out_root / f"trade_date={td}"
                day_dir.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(rows).to_parquet(day_dir / "data.parquet", index=False)
                total_days += 1
                total_rows += len(rows)
                print(f"{td} OK {len(rows)} rows [{total_days} days, {total_rows} total]", flush=True)
        d += timedelta(days=1)

    print(
        f"EXPORT_DONE days={total_days} rows={total_rows} "
        f"skipped_quality_fail={skipped_quality} skipped_no_data={skipped_nodata}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
