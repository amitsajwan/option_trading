#!/usr/bin/env python3
"""Wrap E6's trained stage-1 model into the entry_only_bundle shape the runtime expects.

E6 was trained on stage1_entry_view_v2 (the same view function the runtime invokes via
build_feature_row → project_stage_views_v2). Feature names match runtime, so this
deployment should produce real predictions, not the all-NaN base rate that E1 hit.

Holdout (Aug-Oct 2024) AUC=0.830, Brier=0.170, drift=0.020.
Validation at thr=0.50: 16,785 trades, PF=4.02, WR=68.6%.
Holdout prob distribution: p25=0.30, p50=0.51, p75=0.74, p100=0.995.
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
import pyarrow.parquet as pq

E6_SOURCE = "/opt/option_trading/ml_pipeline_2/artifacts/research/entry_s1_e6_soft50pts_10m_20260523_091300/stages/stage1/model.joblib"
VIEW_DIR = "/opt/option_trading/.data/ml_pipeline/parquet_data/stage1_entry_view_v2"
PUBLISH_DIR = "/opt/option_trading/ml_pipeline_2/artifacts/entry_only/published"
PUBLISH_MODEL = os.path.join(PUBLISH_DIR, "entry_only_model.joblib")
PUBLISH_REPORT = os.path.join(PUBLISH_DIR, "entry_only_report.json")


def main() -> int:
    src = joblib.load(E6_SOURCE)
    features = list(src["feature_columns"])
    model = src["models"]["move"]
    print(f"E6 source loaded: {len(features)} features, model={type(model).__name__}")

    # Compute medians from a 6-month slice (Feb-Jul 2024) — broad coverage, recent enough
    months = [f"2024-{m:02d}" for m in range(2, 8)]
    parts = []
    for y in (2024,):
        year_dir = os.path.join(VIEW_DIR, f"year={y}")
        if not os.path.isdir(year_dir):
            continue
        for f in sorted(os.listdir(year_dir)):
            if not f.endswith(".parquet"):
                continue
            if not any(f.startswith(m) for m in months):
                continue
            p = os.path.join(year_dir, f)
            pf = pq.ParquetFile(p)
            schema = pf.schema_arrow.names
            cols = [c for c in features if c in schema and c != "year"]
            df = pf.read(columns=cols).to_pandas()
            for c in features:
                if c not in df.columns:
                    df[c] = np.nan
            if "year" in features:
                df["year"] = y
            parts.append(df[features])
    df = pd.concat(parts, ignore_index=True)
    print(f"median-computation rows: {len(df)}")

    medians: dict[str, float] = {}
    raw_med = df.median(numeric_only=True)
    for f in features:
        v = raw_med.get(f)
        medians[f] = 0.0 if (v is None or pd.isna(v)) else float(v)
    if "year" in medians:
        medians["year"] = 2024.0

    # Quick sanity: predict on the median-source data
    df_filled = df.copy()
    for c in features:
        df_filled[c] = df_filled[c].fillna(medians[c])
    probs = model.predict_proba(df_filled[features])[:, 1]
    print()
    print("=== SANITY (Feb-Jul 2024 sampled rows) ===")
    print(f"min={probs.min():.4f} mean={probs.mean():.4f} max={probs.max():.4f}")
    for thr in [0.50, 0.55, 0.60, 0.65, 0.70]:
        pct = (probs >= thr).mean() * 100
        print(f"  prob >= {thr:.2f}: {pct:5.2f}%")

    bundle = {
        "kind": "entry_only_bundle",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_run": "entry_s1_e6_soft50pts_10m_20260523_091300",
        "source_description": (
            "E6 single-stage entry model. Soft label: ≥50pts in 10min. "
            "xgb_balanced on stage1_entry_view_v2 (51 features). "
            "Training 2022-01 to 2024-07; holdout Aug-Oct 2024. "
            "Holdout AUC=0.830, drift=0.020. Validation thr=0.50: PF=4.02, WR=68.6%."
        ),
        "features": features,
        "feature_medians": medians,
        "model": model,
        "holdout_eval": {
            "rows": 24059,
            "roc_auc": 0.8296,
            "brier": 0.1696,
            "roc_auc_drift_half_split": 0.0203,
        },
        "training_metadata": {
            "labeler": "entry_bn_5m_100pts_v1",
            "horizon_minutes": 10,
            "min_points": 50,
            "view": "stage1_entry_view_v2",
            "support_dataset": "snapshots_ml_flat_v2",
            "model_name": src.get("selected_model", {}).get("name"),
            "model_params": src.get("selected_model", {}).get("params"),
        },
    }

    os.makedirs(PUBLISH_DIR, exist_ok=True)
    if os.path.exists(PUBLISH_MODEL):
        backup = PUBLISH_MODEL + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(PUBLISH_MODEL, backup)
        print(f"backed up: {backup}")
    if os.path.exists(PUBLISH_REPORT):
        bk = PUBLISH_REPORT + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(PUBLISH_REPORT, bk)
        print(f"backed up: {bk}")

    joblib.dump(bundle, PUBLISH_MODEL)
    print(f"wrote: {PUBLISH_MODEL}")

    report = {
        "exported_at_utc": bundle["created_at_utc"],
        "source_run": bundle["source_run"],
        "description": bundle["source_description"],
        "n_features": len(features),
        "holdout_roc_auc": 0.8296,
        "holdout_brier": 0.1696,
        "holdout_drift": 0.0203,
        "publishable": True,
        "labeler": "entry_bn_5m_100pts_v1 (configured 50pts/10min)",
        "view": "stage1_entry_view_v2",
        "model": "xgb_balanced",
        "validation_at_thr_0.50": {
            "trades": 16785,
            "profit_factor": 4.02,
            "win_rate": 0.686,
            "net_return_sum": 10.93,
        },
        "recommended_min_prob": 0.65,
    }
    with open(PUBLISH_REPORT, "w") as f:
        json.dump(report, f, indent=2)
    print(f"wrote: {PUBLISH_REPORT}")

    # Reload + sanity-check
    print()
    print("--- VERIFY: reload bundle, predict on same data ---")
    b = joblib.load(PUBLISH_MODEL)
    test_df = df[features].copy()
    for c in features:
        test_df[c] = test_df[c].fillna(b["feature_medians"][c])
    probs = b["model"].predict_proba(test_df)[:, 1]
    print(f"reloaded: min={probs.min():.4f} mean={probs.mean():.4f} max={probs.max():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
