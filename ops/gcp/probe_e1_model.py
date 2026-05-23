#!/usr/bin/env python3
"""Probe E1-trained stage1 model on Aug-Oct 2024 holdout.

Verifies:
  1. model loads
  2. features list extracted
  3. predict_proba runs end-to-end
  4. probability distribution is wide enough to fire ML_ENTRY at >= 0.50 threshold
"""
from __future__ import annotations

import os
import sys
import joblib
import numpy as np
import pandas as pd
import pyarrow.dataset as ds

E1 = "/opt/option_trading/ml_pipeline_2/artifacts/research/entry_s1_ablate_e1_c1_repro_20260523_044738/stages/stage1/model.joblib"
SUPPORT = "/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat"


def main() -> int:
    src = joblib.load(E1)
    features = list(src["feature_columns"])
    model = src["models"]["move"]
    print(f"features (n={len(features)}): {features[:5]}...")
    print(f"model: {type(model).__name__}")

    parts = []
    base = os.path.join(SUPPORT, "year=2024")
    chunks = sorted(d for d in os.listdir(base) if d.startswith("chunk="))
    print(f"chunks under year=2024: {chunks}")
    for ch in chunks:
        # Aug-Oct = chunks containing 202408/202409/202410. Pick by name prefix.
        p = os.path.join(base, ch)
        d = ds.dataset(p, format="parquet")
        cols = [c for c in features if c in d.schema.names]
        t = d.to_table(columns=cols)
        df = t.to_pandas()
        if "trade_date" in d.schema.names:
            td = d.to_table(columns=["trade_date"]).to_pandas()
            df["trade_date"] = td["trade_date"].values
        for c in features:
            if c not in df.columns:
                df[c] = np.nan
        if "year" in features:
            df["year"] = 2024
        parts.append(df)

    if not parts:
        print("ERROR: no holdout data found")
        return 1
    df = pd.concat(parts, ignore_index=True)

    if "trade_date" in df.columns:
        # Filter to Aug-Oct 2024 only
        td = pd.to_datetime(df["trade_date"])
        mask = (td >= "2024-08-01") & (td <= "2024-10-31")
        df = df[mask].reset_index(drop=True)
        print(f"filtered to Aug-Oct 2024: {len(df)} rows")
        df = df.drop(columns=["trade_date"])
    print(f"holdout rows: {len(df)}, cols: {df.shape[1]}")

    medians = df.median(numeric_only=True).to_dict()
    df_filled = df.copy()
    for c in features:
        df_filled[c] = df_filled[c].fillna(medians.get(c, 0.0))

    probs = model.predict_proba(df_filled[features])[:, 1]
    print()
    print("=== PROBABILITY DISTRIBUTION (Aug-Oct 2024 holdout) ===")
    print(f"min:  {probs.min():.3f}")
    print(f"p10:  {np.percentile(probs, 10):.3f}")
    print(f"p50:  {np.percentile(probs, 50):.3f}")
    print(f"p90:  {np.percentile(probs, 90):.3f}")
    print(f"max:  {probs.max():.3f}")
    print(f"mean: {probs.mean():.3f}")
    print()
    for thr in [0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70]:
        n = int((probs >= thr).sum())
        pct = (probs >= thr).mean() * 100
        print(f"  prob >= {thr:.2f}: {n:>6} rows ({pct:5.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
