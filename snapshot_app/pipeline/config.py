from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from snapshot_app.historical.snapshot_access import DEFAULT_HISTORICAL_PARQUET_BASE


DEFAULT_RAW_DATA_ROOT = Path(__file__).resolve().parents[3] / "banknifty_data"
DEFAULT_PARQUET_BASE = DEFAULT_HISTORICAL_PARQUET_BASE
DEFAULT_NORMALIZE_JOBS = max(1, min(24, max(1, (os.cpu_count() or 8) - 4)))
DEFAULT_SNAPSHOT_JOBS = max(1, min(8, os.cpu_count() or 4))

RAW_DATASET_DIRS = {
    "futures": "banknifty_fut",
    "options": "banknifty_options",
    "spot": "banknifty_spot",
}
PARTITIONED_DATASETS = frozenset(RAW_DATASET_DIRS.keys())


@dataclass(frozen=True)
class NormalizeTask:
    dataset: str
    year: int
    month: int
    source_dir: Path
    output_path: Path

    @property
    def partition_key(self) -> str:
        return f"{self.dataset}:{self.year:04d}-{self.month:02d}"
