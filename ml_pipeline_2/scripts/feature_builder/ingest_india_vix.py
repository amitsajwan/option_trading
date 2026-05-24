"""Ingest India VIX CSVs into parquet_data/vix/vix.parquet.

Uses the same normalizer as the snapshot pipeline (NSE hist CSV format).

Example (local raw fix):
  python -m ml_pipeline_2.scripts.feature_builder.ingest_india_vix ^
    --vix-root C:/code/banknifty_raw/banknifty_data/vix ^
    --parquet-root .data/ml_pipeline/parquet_data

On ML VM:
  python -m ml_pipeline_2.scripts.feature_builder.ingest_india_vix \\
    --vix-root /path/to/vix \\
    --parquet-root /opt/option_trading/.data/ml_pipeline/parquet_data
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ml_pipeline_2.scripts.feature_builder.regime_daily import resolve_parquet_root
from snapshot_app.pipeline.normalize import normalize_vix_to_parquet


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument(
        "--vix-root",
        required=True,
        help="Directory with hist_india_vix_*.csv files",
    )
    p.add_argument("--parquet-root", default=None, help="parquet_data root (default: resolve_parquet_root())")
    p.add_argument("--force", action="store_true", help="Overwrite existing vix.parquet")
    args = p.parse_args(argv)

    vix_root = Path(args.vix_root)
    if not vix_root.is_dir():
        print(f"vix-root not found: {vix_root}", file=sys.stderr)
        return 1

    parquet_base = resolve_parquet_root(args.parquet_root)
    parquet_base.mkdir(parents=True, exist_ok=True)

    result = normalize_vix_to_parquet(
        raw_root=vix_root.parent,
        parquet_base=parquet_base,
        vix_root=vix_root,
        force=args.force,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in {"written", "skipped_existing"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
