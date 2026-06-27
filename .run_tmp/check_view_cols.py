import sys
sys.path.insert(0, '/home/amits/bmm_run')
from pathlib import Path
import pandas as pd

base = Path("/home/amits/parquet_data/stage1_entry_view_v2/year=2026")
files = sorted(base.glob("*.parquet"))
print(f"Files: {[f.name for f in files]}")

if files:
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    print(f"Shape: {df.shape}")
    print(f"Dates: {sorted(df['trade_date'].unique())}")
    check_cols = [
        "px_fut_close", "px_fut_open", "px_fut_high", "px_fut_low",
        "vel_range_pct", "vel_close_pct", "vel_momentum_pct",
        "ctx_am_range_pct", "ctx_gap_pct", "adx_14", "vol_spike_ratio",
        "bmm_ema_compression_score", "bmm_atr_ratio",
        "timestamp", "trade_date", "snapshot_id",
    ]
    for col in check_cols:
        if col in df.columns:
            nn = df[col].notna().sum()
            print(f"  {col}: {nn}/{len(df)}")
        else:
            print(f"  {col}: MISSING")
