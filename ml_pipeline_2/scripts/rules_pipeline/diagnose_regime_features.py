"""Per-quarter regime diagnostic.

For each window in the 17-quarter sweep, load the flat v3 data and
print mean/median of vol-related features. Tag each row with the
R1S baseline verdict (PASS / FAIL_NEAR / FAIL_DEEP) so we can eyeball
which feature distribution actually separates the regime groups.

Run on the ML VM:
    /opt/option_trading/.venv/bin/python -m \
      ml_pipeline_2.scripts.rules_pipeline.diagnose_regime_features
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

import pandas as pd

from ml_pipeline_2.scripts.feature_builder.regime_daily import ALL_REGIME_COLUMNS, resolve_parquet_root

# (window_name, start, end, R1S baseline verdict)
# verdict from r1s_history leaderboard:
#   PASS   — t > 2, all gates clear
#   FAIL+  — t > 0 but missed gates (regime is still mostly fine)
#   FAIL-  — t < 0 OR net_w/o_top5 deeply negative (the regime we want to block)
WINDOWS = [
    ("2020_aug_dec", "2020-08-03", "2020-12-31", "PASS"),
    ("2021_q1",      "2021-01-01", "2021-03-31", "PASS"),
    ("2021_q2",      "2021-04-01", "2021-06-30", "FAIL+"),
    ("2021_q3",      "2021-07-01", "2021-09-30", "FAIL+"),
    ("2021_q4",      "2021-10-01", "2021-12-31", "PASS"),
    ("2022_q1",      "2022-01-01", "2022-03-31", "FAIL-"),
    ("2022_q2",      "2022-04-01", "2022-06-30", "FAIL+"),
    ("2022_q3",      "2022-07-01", "2022-09-30", "FAIL-"),
    ("2022_q4",      "2022-10-01", "2022-12-31", "FAIL-"),
    ("2023_q1",      "2023-01-01", "2023-03-31", "FAIL-"),
    ("2023_q2",      "2023-04-01", "2023-06-30", "FAIL-"),
    ("2023_q3",      "2023-07-01", "2023-09-30", "PASS"),
    ("2023_q4",      "2023-10-01", "2023-12-31", "FAIL-"),
    ("2024_q1",      "2024-01-01", "2024-03-31", "PASS"),
    ("2024_q2",      "2024-04-01", "2024-06-30", "PASS"),
    ("2024_q3",      "2024-07-01", "2024-09-30", "FAIL-"),
    ("2024_oct",     "2024-10-01", "2024-10-31", "FAIL-"),
]

# Intraday vol / regime (per-minute)
INTRADAY_FEATURES = [
    "osc_atr_14",
    "osc_atr_ratio",
    "osc_atr_percentile",
    "osc_atr_daily_percentile",
    "fut_flow_oi_zscore_20",
    "ctx_is_high_vix_day",
    "ctx_regime_atr_high",
    "ctx_regime_atr_low",
    "ctx_regime_trend_up",
    "ctx_regime_trend_down",
]

DAILY_REGIME_FEATURES = list(ALL_REGIME_COLUMNS)

FEATURES: List[str] = INTRADAY_FEATURES + DAILY_REGIME_FEATURES


def _flat_root(explicit: Optional[str] = None) -> Path:
    return resolve_parquet_root(explicit) / "snapshots_ml_flat_v3"


def _load_quarter(flat_root: Path, start: str, end: str) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    years = set(range(start_ts.year, end_ts.year + 1))
    frames = []
    for y in sorted(years):
        for f in sorted(flat_root.glob(f"year={y}/*.parquet")):
            df = pd.read_parquet(f, columns=["trade_date"] + FEATURES)
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            mask = (df["trade_date"] >= start_ts) & (df["trade_date"] <= end_ts)
            if mask.any():
                frames.append(df[mask])
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--parquet-root", default=None, help="parquet_data root")
    args = p.parse_args(argv)

    flat_root = _flat_root(args.parquet_root)
    if not flat_root.exists():
        raise SystemExit(f"flat v3 not found: {flat_root}")

    rows = []
    for name, start, end, verdict in WINDOWS:
        df = _load_quarter(flat_root, start, end)
        if df.empty:
            print(f"{name}: empty")
            continue
        record = {"window": name, "verdict": verdict, "n_rows": len(df),
                  "n_days": df["trade_date"].nunique()}
        for c in FEATURES:
            if c not in df.columns:
                record[c] = None
                continue
            s = pd.to_numeric(df[c], errors="coerce").dropna()
            if len(s) == 0:
                record[c] = None
                continue
            # For binary flags report fire-rate; for continuous report median
            if c.startswith("ctx_is_") or c.startswith("ctx_regime_"):
                record[c] = round(float(s.mean()), 3)
            else:
                record[c] = round(float(s.median()), 4)
        rows.append(record)

    out = pd.DataFrame(rows)
    out = out.sort_values(["verdict", "window"])
    # Print as a table grouped by verdict so contrast is obvious
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 220)
    print(out.to_string(index=False))

    print()
    print("=== group means (PASS vs FAIL-) ===")
    grp = out.groupby("verdict")[FEATURES].mean(numeric_only=True).round(4)
    print(grp.to_string())

    if DAILY_REGIME_FEATURES[0] in out.columns and out[DAILY_REGIME_FEATURES[0]].notna().any():
        print()
        print("=== daily regime: PASS vs FAIL- (window medians) ===")
        sub = out[out["verdict"].isin(["PASS", "FAIL-"])][["verdict"] + DAILY_REGIME_FEATURES]
        print(sub.groupby("verdict")[DAILY_REGIME_FEATURES].median(numeric_only=True).round(4).to_string())


if __name__ == "__main__":
    main()
