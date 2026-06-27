import pandas as pd
from pathlib import Path

# Check snapshots_ml_flat_v2 for 2026 
base = Path("/home/amits/parquet_data/snapshots_ml_flat_v2/year=2026")
df = pd.concat([pd.read_parquet(f) for f in sorted(base.glob("*.parquet"))], ignore_index=True)
print(f"snapshots_ml_flat_v2 year=2026: {df.shape}")
for col in ["px_fut_close", "px_fut_high", "px_fut_low", "px_fut_open",
            "vel_range_pct", "vel_close_pct", "vel_momentum_pct",
            "ctx_am_range_pct", "ctx_am_vol_mean", "vol_spike_ratio", "adx_14"]:
    if col in df.columns:
        nn = df[col].notna().sum()
        print(f"  {col}: {nn}/{len(df)}")
    else:
        print(f"  {col}: MISSING")
