#!/usr/bin/env python3
"""Summarize PnL from rules_pipeline smoke/monthly run cells."""
import json
import sys
from pathlib import Path

import pandas as pd

root = Path(sys.argv[1])
for cell in sorted((root / "cells").iterdir()):
    audit_path = cell / "audit.json"
    trades_path = cell / "trades.parquet"
    if not audit_path.exists() or not trades_path.exists():
        continue
    aud = json.loads(audit_path.read_text())
    df = pd.read_parquet(trades_path)
    daily = aud.get("daily") or {}
    st = aud.get("stats") or {}
    ci = aud.get("ci") or {}
    sum_pct = float(df["net_pnl_pct"].sum()) * 100
    avg_pct = float(df["net_pnl_pct"].mean()) * 100 if len(df) else 0.0
    # Example: 200 premium, 1 lot BNF 15 -> rough Rs per trade
    print(
        f"{cell.name[:50]:50} "
        f"n={len(df):3d} "
        f"sum={sum_pct:+7.2f}% "
        f"avg/trade={avg_pct:+6.2f}% "
        f"wr={aud.get('win_rate', 0)*100:5.1f}% "
        f"t={st.get('t', 0):+.2f} "
        f"ci=[{ci.get('ci_lo', 0)*100:+.2f}%,{ci.get('ci_hi', 0)*100:+.2f}%] "
        f"daily_net={daily.get('net_without_top5_days', 0)*100:+.1f}%"
    )
