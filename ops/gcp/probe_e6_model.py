#!/usr/bin/env python3
"""Probe E6's trained stage1 model on Aug-Oct 2024 holdout.

E6 trained on snapshots_ml_flat_v2 + stage1_entry_view_v2 — same view the
runtime uses — so feature names should match at inference.
"""
from __future__ import annotations

import os
import sys
import joblib
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

E6 = "/opt/option_trading/ml_pipeline_2/artifacts/research/entry_s1_e6_soft50pts_10m_20260523_091300/stages/stage1/model.joblib"
V2_DIR = "/opt/option_trading/.data/ml_pipeline/parquet_data/stage1_entry_view_v2/year=2024"


def main() -> int:
    src = joblib.load(E6)
    features = list(src["feature_columns"])
    model = src["models"]["move"]
    print(f"E6 features (n={len(features)}): {features[:5]}...")
    print(f"model: {type(model).__name__}")

    # Read Aug-Oct 2024 holdout from v2 dataset
    files = sorted([f for f in os.listdir(V2_DIR) if f.endswith(".parquet")])
    aug_oct = [f for f in files if f.startswith("2024-08") or f.startswith("2024-09") or f.startswith("2024-10")]
    print(f"loading {len(aug_oct)} files from Aug-Oct 2024 ...")

    dfs = []
    for f in aug_oct:
        p = os.path.join(V2_DIR, f)
        pf = pq.ParquetFile(p)
        schema = pf.schema_arrow.names
        cols = [c for c in features if c in schema and c != "year"]
        df = pf.read(columns=cols).to_pandas()
        for c in features:
            if c not in df.columns:
                df[c] = np.nan
        if "year" in features:
            df["year"] = 2024
        dfs.append(df[features])
    df = pd.concat(dfs, ignore_index=True)
    print(f"holdout rows: {len(df)}")

    # Coverage: what fraction of features have ANY value
    cov = []
    for f in features:
        nn = df[f].notna().sum()
        cov.append((f, nn, len(df)))
    missing_features = [f for f, nn, n in cov if nn == 0]
    print(f"features with ALL-null in holdout: {len(missing_features)}")
    if missing_features:
        print(f"  examples: {missing_features[:10]}")
    partial = [(f, nn, n) for f, nn, n in cov if 0 < nn < n]
    print(f"features with partial coverage: {len(partial)}")
    if partial:
        for f, nn, n in partial[:5]:
            print(f"  {f}: {nn}/{n}")

    medians = df.median(numeric_only=True).to_dict()
    for f in features:
        if f not in medians or pd.isna(medians.get(f)):
            medians[f] = 0.0
    if "year" in medians:
        medians["year"] = 2024.0
    df_filled = df.copy()
    for f in features:
        df_filled[f] = df_filled[f].fillna(medians[f])

    probs = model.predict_proba(df_filled[features])[:, 1]
    print()
    print("=== PROBABILITY DISTRIBUTION (Aug-Oct 2024 holdout) ===")
    for q in [0, 1, 5, 10, 25, 50, 75, 90, 95, 99, 100]:
        print(f"  p{q:>3}: {np.percentile(probs, q):.4f}")
    print(f"  mean: {probs.mean():.4f}")
    print()
    for thr in [0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        n = int((probs >= thr).sum())
        pct = (probs >= thr).mean() * 100
        print(f"  prob >= {thr:.2f}: {n:>6} rows ({pct:5.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
