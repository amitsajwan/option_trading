import pandas as pd
from pathlib import Path

base = Path("/home/amits/parquet_data/stage1_entry_view_v2/year=2026")
df = pd.concat([pd.read_parquet(f) for f in sorted(base.glob("*.parquet"))], ignore_index=True)
print(f"Shape: {df.shape}")
print("All columns (name: non-null count):")
for col in sorted(df.columns):
    nn = df[col].notna().sum()
    print(f"  {col}: {nn}/{len(df)}")
