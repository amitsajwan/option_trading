import sys
sys.path.insert(0, "/opt/option_trading")
from ml_pipeline_2.scripts.train_option_pnl_mvp import load_labels_and_features, split_temporal
import pandas as pd

from pathlib import Path
df = load_labels_and_features(
    Path("/opt/option_trading/.data/ml_pipeline/parquet_data/option_pnl_labels_v1"),
    Path("/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2"),
    "ATM_PE_9"
)
_, _, ho = split_temporal(df)
print("columns:", list(ho.columns[:8]))
print("rows:", len(ho))
print("trade_date dtype:", ho["trade_date"].dtype if "trade_date" in ho.columns else "MISSING")
print("timestamp in cols:", "timestamp" in ho.columns)
print("sample trade_date:", ho["trade_date"].iloc[0] if "trade_date" in ho.columns else "N/A")
print("sample timestamp:", ho["timestamp"].iloc[0] if "timestamp" in ho.columns else "N/A")
print("index type:", type(ho.index))
print("unique trade_dates:", ho["trade_date"].nunique() if "trade_date" in ho.columns else "N/A")
