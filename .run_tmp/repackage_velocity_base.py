"""
Re-package velocity_base research bundle → entry_only_bundle format
so the live loader (load_joblib_bundle) accepts it.

Key renames:
  feature_columns   → features
  models["move"]    → model
  kind              → "entry_only_bundle"

Output: /home/amits/velocity_base_entry_bundle.joblib
"""
import sys, joblib
sys.path.insert(0, "/home/amits/bmm_run/ml_pipeline_2/src")
sys.path.insert(0, "/home/amits/bmm_run")
import numpy as np
import pandas as pd
from pathlib import Path

SRC = sorted(
    Path("/home/amits/bmm_run/ml_pipeline_2/artifacts/research")
    .glob("ab_5m020_base_*/stages/stage1/model.joblib")
)[0]
OUT = Path("/home/amits/velocity_base_entry_bundle.joblib")

print(f"Loading: {SRC}")
src = joblib.load(SRC)

features = list(src["feature_columns"])          # 54 features
model    = src["models"]["move"]                  # sklearn Pipeline
holdout  = src.get("trading_utility_config") or {}

# Compute feature medians from the stage1_entry_view_v2 2026 data for NaN fallback
import duckdb, os
VIEW_ROOT = "/home/amits/parquet_data/stage1_entry_view_v2"
try:
    con = duckdb.connect(":memory:")
    df = con.execute(
        f"SELECT * FROM read_parquet('{VIEW_ROOT}/**/*.parquet',"
        f" hive_partitioning=false, union_by_name=true)"
    ).df()
    con.close()
    feature_medians = {}
    for f in features:
        if f in df.columns:
            v = pd.to_numeric(df[f], errors="coerce")
            feature_medians[f] = float(v.median()) if v.notna().any() else 0.0
        else:
            feature_medians[f] = 0.0
    print(f"Computed medians from {len(df)} rows")
except Exception as e:
    print(f"WARNING: could not compute medians ({e}), using 0.0")
    feature_medians = {f: 0.0 for f in features}

bundle = {
    "kind":             "entry_only_bundle",
    "features":         features,
    "model":            model,
    "feature_medians":  feature_medians,
    "source_bundle":    str(SRC),
    "source_kind":      src.get("kind"),
    "feature_profile":  src.get("feature_profile"),
    "selected_model":   src.get("selected_model"),
    "created_at_utc":   src.get("created_at_utc"),
}

joblib.dump(bundle, OUT, compress=3)
print(f"Saved: {OUT}")

# Quick smoke-test: single-row predict
row = {f: feature_medians.get(f, 0.0) for f in features}
frame = pd.DataFrame([row], columns=features)
prob = float(bundle["model"].predict_proba(frame)[0, 1])
print(f"Smoke-test predict_proba (median row): {prob:.5f}")
print(f"Expected range: 0.03 – 0.09 (top-10% threshold = 0.049)")
print("Done.")
