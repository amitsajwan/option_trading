#!/usr/bin/env python3
"""Print exit_reason counts per cell under a rules run output root."""
import sys
from pathlib import Path

import pandas as pd

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
for cell in sorted((root / "cells").iterdir()):
    p = cell / "trades.parquet"
    if not p.exists():
        continue
    df = pd.read_parquet(p)
    vc = df["exit_reason"].value_counts().to_dict()
    print(f"{cell.name}  n={len(df)}  {vc}")
