import pandas as pd
from pathlib import Path

base = Path("/home/amits/parquet_data/snapshots_ml_flat_v2/year=2026")
df = pd.concat([pd.read_parquet(f) for f in sorted(base.glob("*.parquet"))], ignore_index=True)
print(f"Shape: {df.shape}")
print("Columns with non-null on 11:30 row:")
# Filter to 11:30 rows (where adx_14 is non-null)
midday = df[df["adx_14"].notna()]
print(f"  11:30 rows: {len(midday)}")
print("All velocity/context-related columns:")
vel_cols = [c for c in sorted(df.columns) if any(
    c.startswith(p) for p in ["vel_", "ctx_", "adx", "vol_spike"]
)]
for col in vel_cols:
    nn_midday = midday[col].notna().sum() if col in midday.columns else 0
    nn_all = df[col].notna().sum()
    print(f"  {col}: {nn_midday}/{len(midday)} midday, {nn_all}/{len(df)} total")
