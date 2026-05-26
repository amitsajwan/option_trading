"""Check if snapshots_ml_flat has snapshot_raw_json column."""
import pandas as pd
from pathlib import Path

base = Path("/opt/option_trading/.data/ml_pipeline/parquet_data")
f = base / "snapshots_ml_flat/year=2020/chunk=202007_202012_m6/data.parquet"
df = pd.read_parquet(f)
cols = list(df.columns)
print("rows:", len(df))
print("has snapshot_raw_json:", "snapshot_raw_json" in cols)
print("cols sample:", cols[:30])
