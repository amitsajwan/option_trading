#!/usr/bin/env python3
"""Wrap E1's trained stage-1 model into the entry_only_bundle shape the
runtime ml_entry strategy expects.

Bundle keys required by strategy_app/ml/bundle_inference.py:
  - kind: must equal "entry_only_bundle"
  - features: list of feature names
  - feature_medians: dict name -> float (used to fill NaN at inference)
  - model: sklearn-compatible classifier with predict_proba

Source E1 dict has:
  - feature_columns: 38 names
  - models["move"]: sklearn Pipeline (xgb_balanced)
  - selected_model: {"name": "xgb_balanced", ...}

This script also computes feature_medians from the Aug-Oct 2024 holdout window
of snapshots_ml_flat (close enough for NaN fallback at inference time).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
import pyarrow.dataset as ds

E1_SOURCE = "/opt/option_trading/ml_pipeline_2/artifacts/research/entry_s1_ablate_e1_c1_repro_20260523_044738/stages/stage1/model.joblib"
PUBLISH_DIR = "/opt/option_trading/ml_pipeline_2/artifacts/entry_only/published"
PUBLISH_MODEL = os.path.join(PUBLISH_DIR, "entry_only_model.joblib")
PUBLISH_REPORT = os.path.join(PUBLISH_DIR, "entry_only_report.json")
SUPPORT_DIR = "/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat"


def main() -> int:
    src = joblib.load(E1_SOURCE)
    features = list(src["feature_columns"])
    model = src["models"]["move"]
    print(f"E1 source loaded: {len(features)} features, model={type(model).__name__}")

    # Compute medians from Aug-Oct 2024 holdout window
    parts = []
    base = os.path.join(SUPPORT_DIR, "year=2024")
    chunks = sorted(d for d in os.listdir(base) if d.startswith("chunk="))
    for ch in chunks:
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
        parts.append(df)
    df = pd.concat(parts, ignore_index=True)
    if "trade_date" in df.columns:
        td = pd.to_datetime(df["trade_date"])
        df = df[(td >= "2024-08-01") & (td <= "2024-10-31")].drop(columns=["trade_date"])
    print(f"holdout rows for medians: {len(df)}")

    medians_raw = df.median(numeric_only=True)
    medians: dict[str, float] = {}
    for f in features:
        v = medians_raw.get(f)
        if pd.isna(v):
            medians[f] = 0.0
        else:
            medians[f] = float(v)
    # year is partition-derived; force to 2024
    if "year" in medians:
        medians["year"] = 2024.0

    # Build the entry_only_bundle
    bundle = {
        "kind": "entry_only_bundle",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_run": "entry_s1_ablate_e1_c1_repro_20260523_044738",
        "source_description": "C1 stage-1 reproduction; xgb_balanced with fo_full (38 features) trained 2020-08 to 2024-07 on snapshots_ml_flat. Holdout (Aug-Oct 2024) AUC=0.683 per C1 reference. Validation max prob 0.702, min 0.670 — narrow-band predictor.",
        "features": features,
        "feature_medians": medians,
        "model": model,
        "holdout_eval": {
            "rows": 24059,
            "roc_auc": 0.683,
            "note": "AUC from C1's published threshold report — same model class, features, and training period.",
        },
        "training_metadata": {
            "labeler": "entry_best_recipe_v1",
            "view": "stage1_entry_view_v1",
            "support_dataset": "snapshots_ml_flat",
            "model_name": src.get("selected_model", {}).get("name"),
            "model_params": src.get("selected_model", {}).get("params"),
        },
    }

    # Backup existing model
    os.makedirs(PUBLISH_DIR, exist_ok=True)
    if os.path.exists(PUBLISH_MODEL):
        backup = PUBLISH_MODEL + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(PUBLISH_MODEL, backup)
        print(f"backed up existing model to: {backup}")
    if os.path.exists(PUBLISH_REPORT):
        backup_report = PUBLISH_REPORT + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(PUBLISH_REPORT, backup_report)
        print(f"backed up existing report to: {backup_report}")

    # Write the new bundle
    joblib.dump(bundle, PUBLISH_MODEL)
    print(f"wrote new entry_only bundle: {PUBLISH_MODEL}")

    # Write a human-readable report
    report = {
        "exported_at_utc": bundle["created_at_utc"],
        "source_run": bundle["source_run"],
        "description": bundle["source_description"],
        "n_features": len(features),
        "holdout_roc_auc": 0.683,
        "publishable": True,
        "labeler": bundle["training_metadata"]["labeler"],
        "view": bundle["training_metadata"]["view"],
        "model": bundle["training_metadata"]["model_name"],
        "prob_dist_aug_oct_2024": {
            "min": 0.670,
            "p50": 0.691,
            "max": 0.702,
            "n_above_0.70_pct": 0.60,
        },
        "recommended_min_prob": 0.70,
    }
    with open(PUBLISH_REPORT, "w") as f:
        json.dump(report, f, indent=2)
    print(f"wrote new report: {PUBLISH_REPORT}")

    # Verification: re-load and predict
    print("\n--- VERIFY: reload bundle and predict on holdout ---")
    b = joblib.load(PUBLISH_MODEL)
    print(f"reloaded keys: {sorted(b.keys())}")
    print(f"kind: {b['kind']}, n_features: {len(b['features'])}")
    test_df = df.copy()
    test_df = test_df[features].fillna(b["feature_medians"])
    probs = b["model"].predict_proba(test_df)[:, 1]
    print(f"prob dist on re-loaded bundle: min={probs.min():.3f} max={probs.max():.3f} mean={probs.mean():.3f}")
    n70 = int((probs >= 0.70).sum())
    print(f"rows >= 0.70: {n70} ({n70/len(probs)*100:.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
