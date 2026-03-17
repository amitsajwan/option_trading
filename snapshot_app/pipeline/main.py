from __future__ import annotations

import argparse
import json
from pathlib import Path

from snapshot_app.historical.snapshot_access import DEFAULT_HISTORICAL_PARQUET_BASE

from .config import DEFAULT_NORMALIZE_JOBS, DEFAULT_RAW_DATA_ROOT, DEFAULT_SNAPSHOT_JOBS
from .normalize import normalize_raw_to_parquet
from .orchestrator import run_snapshot_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="Raw-to-snapshot pipeline for BankNifty historical data.")
    sub = parser.add_subparsers(dest="command", required=True)

    normalize = sub.add_parser("normalize", help="Normalize raw CSV inputs into parquet cache.")
    normalize.add_argument("--raw-root", default=str(DEFAULT_RAW_DATA_ROOT))
    normalize.add_argument("--parquet-base", default=str(DEFAULT_HISTORICAL_PARQUET_BASE))
    normalize.add_argument("--vix-root", default=None)
    normalize.add_argument("--jobs", type=int, default=DEFAULT_NORMALIZE_JOBS)
    normalize.add_argument("--force", action="store_true")

    run = sub.add_parser("run", help="Normalize raw data and build canonical snapshots.")
    run.add_argument("--raw-root", default=str(DEFAULT_RAW_DATA_ROOT))
    run.add_argument("--parquet-base", default=str(DEFAULT_HISTORICAL_PARQUET_BASE))
    run.add_argument("--vix-root", default=None)
    run.add_argument("--normalize-jobs", type=int, default=DEFAULT_NORMALIZE_JOBS)
    run.add_argument("--snapshot-jobs", type=int, default=DEFAULT_SNAPSHOT_JOBS)
    run.add_argument("--force-normalize", action="store_true")
    run.add_argument("--normalize-only", action="store_true")
    run.add_argument("--instrument", default="BANKNIFTY-I")
    run.add_argument("--min-day", default=None)
    run.add_argument("--max-day", default=None)
    run.add_argument("--lookback-days", type=int, default=30)
    run.add_argument("--no-resume", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--log-every", type=int, default=10)
    run.add_argument("--write-batch-days", type=int, default=20)
    run.add_argument("--build-source", default="historical")
    run.add_argument("--build-run-id", default=None)
    run.add_argument("--validate-ml-flat-contract", action="store_true")

    args = parser.parse_args()
    if args.command == "normalize":
        payload = normalize_raw_to_parquet(
            raw_root=Path(args.raw_root),
            parquet_base=Path(args.parquet_base),
            vix_root=(Path(args.vix_root) if args.vix_root else None),
            jobs=int(args.jobs),
            force=bool(args.force),
        )
    else:
        payload = run_snapshot_pipeline(
            raw_root=Path(args.raw_root),
            parquet_base=Path(args.parquet_base),
            vix_root=(Path(args.vix_root) if args.vix_root else None),
            normalize_jobs=int(args.normalize_jobs),
            snapshot_jobs=int(args.snapshot_jobs),
            force_normalize=bool(args.force_normalize),
            normalize_only=bool(args.normalize_only),
            instrument=args.instrument,
            min_day=args.min_day,
            max_day=args.max_day,
            lookback_days=int(args.lookback_days),
            resume=(not args.no_resume),
            dry_run=bool(args.dry_run),
            log_every=int(args.log_every),
            write_batch_days=int(args.write_batch_days),
            build_source=args.build_source,
            build_run_id=args.build_run_id,
            validate_ml_flat_contract=bool(args.validate_ml_flat_contract),
        )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
