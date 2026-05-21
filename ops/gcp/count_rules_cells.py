#!/usr/bin/env python3
"""Count trades in playbook monthly cells for holdout months."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else "/opt/option_trading/ml_pipeline_2/artifacts/rules_runs/playbook_v1_monthly_20260521/cells"
)
RULE = sys.argv[2] if len(sys.argv) > 2 else "PBV1_TOP3_THESIS"
MONTHS = sys.argv[3:] if len(sys.argv) > 3 else [
    "2024_05", "2024_06", "2024_07", "2024_08", "2024_09", "2024_10",
]

total = 0
for month in MONTHS:
    path = ROOT / f"{RULE}_{month}_mechanical" / "trades.parquet"
    if not path.is_file():
        print(f"{month}: missing {path}")
        continue
    n = len(pd.read_parquet(path))
    total += n
    print(f"{month}: n={n}")
print(f"TOTAL {RULE}: {total}")
