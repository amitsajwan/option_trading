from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EXTERNAL_DATA_ROOT = REPO_ROOT / ".data" / "ml_pipeline"
PARQUET_DATA_ROOT = EXTERNAL_DATA_ROOT / "parquet_data"
RUN_ROOT = REPO_ROOT / ".run" / "offline_ml"
ARTIFACTS_ROOT = RUN_ROOT / "artifacts"
DATA_ROOT = RUN_ROOT / "data"

