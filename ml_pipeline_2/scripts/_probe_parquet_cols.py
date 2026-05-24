"""One-off: print parquet columns for a sample file."""
import sys
from pathlib import Path
import pandas as pd

root = Path(sys.argv[1])
for name in ("snapshots_ml_flat_v2", "snapshots_ml_flat_v3", "snapshots_ml_flat"):
    d = root / name
    if not d.exists():
        continue
    files = sorted(d.glob("year=2024/*.parquet"))
    if not files:
        continue
    df = pd.read_parquet(files[0])
    print(f"\n=== {name} ({files[0].name}) cols={len(df.columns)} rows={len(df)} ===")
    cols = list(df.columns)
    print("sample:", cols[:40])
    vel = [c for c in cols if c.startswith("vel_") or c.startswith("ctx_am")]
    print("velocity cols:", vel[:15], "..." if len(vel) > 15 else "")
    px = [c for c in cols if "fut" in c.lower() or c.startswith("px_")]
    print("price cols:", px[:10])
