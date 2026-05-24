"""Grid daily regime disqualifier thresholds vs R1S 17-quarter PASS/FAIL labels.

Run on ML VM after build_daily_regime_v3 backfill:
  python -m ml_pipeline_2.scripts.rules_pipeline.sweep_regime_thresholds
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from ml_pipeline_2.scripts.feature_builder.regime_daily import ALL_REGIME_COLUMNS, resolve_parquet_root
from ml_pipeline_2.scripts.rules_pipeline.diagnose_regime_features import WINDOWS

# Day-level: fraction of minutes where regime would block (proxy for "would we trade")
PASS_VERDICTS = {"PASS"}
FAIL_MINUS = {"FAIL-"}


def _flat_root(explicit: str | None) -> Path:
    root = resolve_parquet_root(explicit)
    return root / "snapshots_ml_flat_v3"


def _daily_regime_for_window(flat_root: Path, start: str, end: str) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    years = set(range(start_ts.year, end_ts.year + 1))
    frames = []
    cols = ["trade_date"] + ALL_REGIME_COLUMNS
    for y in sorted(years):
        for f in sorted(flat_root.glob(f"year={y}/*.parquet")):
            try:
                df = pd.read_parquet(f, columns=cols)
            except Exception:
                df = pd.read_parquet(f)
                df = df[[c for c in cols if c in df.columns]]
            if "trade_date" not in df.columns:
                continue
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
            mask = (df["trade_date"] >= start_ts) & (df["trade_date"] <= end_ts)
            if mask.any():
                frames.append(df.loc[mask].drop_duplicates("trade_date"))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates("trade_date")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--parquet-root", default=None)
    args = p.parse_args()
    flat_root = _flat_root(args.parquet_root)
    if not flat_root.exists():
        raise SystemExit(f"flat v3 not found: {flat_root}")

    rv_thresholds = [0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26]
    rows = []
    for rv_max in rv_thresholds:
        pass_days, failm_days, pass_total, failm_total = 0, 0, 0, 0
        for _name, start, end, verdict in WINDOWS:
            daily = _daily_regime_for_window(flat_root, start, end)
            if daily.empty:
                continue
            block = (daily["regime_rv20"] > rv_max) | (daily["regime_sma20_slope"] <= 0)
            block = block.fillna(True)
            n_days = len(daily)
            n_block = int(block.sum())
            if verdict in PASS_VERDICTS:
                pass_total += n_days
                pass_days += n_block
            elif verdict in FAIL_MINUS:
                failm_total += n_days
                failm_days += n_block
        rows.append(
            {
                "rv_max": rv_max,
                "slope_gt_0": True,
                "pass_block_pct": round(100 * pass_days / pass_total, 1) if pass_total else None,
                "failm_block_pct": round(100 * failm_days / failm_total, 1) if failm_total else None,
            }
        )

    out = pd.DataFrame(rows)
    print(out.to_string(index=False))
    print()
    print("Target: low pass_block_pct, high failm_block_pct")


if __name__ == "__main__":
    main()
