#!/usr/bin/env python3
"""E2-S8: Check snapshot parquet date coverage for OOS windows."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

PARQUET_ROOT = Path(__import__("os").environ.get(
    "PARQUET_ROOT",
    "/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2",
))

WINDOWS = {
    "oos_primary": (date(2024, 5, 1), date(2024, 7, 31)),
    "oos_secondary": (date(2023, 5, 1), date(2023, 7, 31)),
    "in_sample_sanity": (date(2024, 8, 1), date(2024, 10, 31)),
}


def partition_exists(d: date) -> bool:
    iso = d.isoformat()
    year = d.year
    candidates = [
        PARQUET_ROOT / f"trade_date={iso}",
        PARQUET_ROOT / f"year={year}" / f"trade_date={iso}",
        PARQUET_ROOT / f"year={year}" / f"date={iso}",
    ]
    for part in candidates:
        if part.is_dir() and any(part.glob("*.parquet")):
            return True
    if any(PARQUET_ROOT.rglob(f"**/trade_date={iso}/*.parquet")):
        return True
    if any(PARQUET_ROOT.rglob(f"**/date={iso}/*.parquet")):
        return True
    return False


def count_partitions(start: date, end: date) -> tuple[int, int, list[str]]:
    missing: list[str] = []
    total = 0
    found = 0
    d = start
    while d <= end:
        total += 1
        if partition_exists(d):
            found += 1
        else:
            missing.append(d.isoformat())
        d += timedelta(days=1)
    return found, total, missing[:15]


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else PARQUET_ROOT
    print(f"parquet_root: {root}")
    if not root.exists():
        print("ERROR: root does not exist")
        return 1

    for label, (start, end) in WINDOWS.items():
        found, total, missing_sample = count_partitions(start, end)
        ok = found == total
        print(f"\n{label}: {start} -> {end}")
        print(f"  partitions: {found}/{total}  {'OK' if ok else 'GAPS'}")
        if missing_sample:
            print(f"  missing sample: {missing_sample}")
            if len(missing_sample) == 15:
                print("  ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
