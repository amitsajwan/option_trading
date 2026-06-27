import pandas as pd
df = pd.read_parquet("/home/amits/parquet_data/snapshots_ml_flat/year=2026/data.parquet")
print(f"Shape: {df.shape}")
print("\nAll columns:")
for c in sorted(df.columns):
    nn = df[c].notna().sum()
    print(f"  {c}: {nn}/{len(df)}")
