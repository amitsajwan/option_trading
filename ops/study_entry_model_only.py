#!/usr/bin/env python3
"""Fast, filter-free entry-model-only analysis (2026-07-15, user request):
score the LIVE entry model directly against the (now feature-complete)
training view, with NO regime/IV_FILTER/strategy-router logic at all —
just "at this threshold, how often does the model fire, and when it does,
how often did the labeled move actually happen." The only filter applied
is the model's own feature-completeness gate (max_nan_features), since
evaluating on data the model itself would refuse live is not a fair test.

This is orders of magnitude faster than a real-engine replay because it's
one vectorized predict_proba() over the whole historical matrix instead of
rebuilding a live snapshot bar-by-bar.

Usage: python ops/study_entry_model_only.py BANKNIFTY
       python ops/study_entry_model_only.py NIFTY
"""
import sys
import joblib
import numpy as np
import pandas as pd

instrument = sys.argv[1].upper() if len(sys.argv) > 1 else "BANKNIFTY"

MODEL_PATH = {
    "BANKNIFTY": "/app/models/dhan_entry_bundle_v3.joblib",
    "NIFTY": "/app/models/nifty_entry_bundle_v3.joblib",
}[instrument]
VIEW_PATH = {
    "BANKNIFTY": "/app/.run/training_view/training_view_banknifty.csv.gz",
    "NIFTY": "/app/.run/training_view/training_view_nifty.csv.gz",
}[instrument]
LIVE_THRESHOLD = 0.55

print(f"=== {instrument} entry-model-only study ===", flush=True)
b = joblib.load(MODEL_PATH)
features = b["features"]
medians = b["medians"]
model = b["model"]
max_nan = b.get("max_nan_features", 3)
print(f"model: {MODEL_PATH}")
print(f"features: {len(features)}  max_nan_features={max_nan}  "
      f"live_threshold={LIVE_THRESHOLD}  label={b.get('label_definition')}")

df = pd.read_csv(VIEW_PATH, compression="infer")
print(f"training view rows: {len(df)}")

for f in features:
    if f not in df.columns:
        df[f] = np.nan

X_raw = df[features].apply(pd.to_numeric, errors="coerce")
nan_count = X_raw.isna().sum(axis=1)
ok = nan_count <= max_nan
print(f"rows passing feature-completeness (<= {max_nan} NaN of {len(features)}): "
      f"{ok.sum()} / {len(df)} ({100*ok.mean():.1f}%)")

X = X_raw.copy()
for f in features:
    X[f] = X[f].fillna(medians.get(f, 0.0))
X = X[ok].reset_index(drop=True)
sub = df[ok].reset_index(drop=True)

proba = model.predict_proba(X)[:, 1]
sub = sub.assign(model_prob=proba)

# The model's own label: |move| >= 100pt within 15min of THIS bar.
label_col = "fwd_maxabs_15m"
if label_col not in sub.columns:
    print(f"WARNING: {label_col} not in training view, available fwd_ cols: "
          f"{[c for c in sub.columns if c.startswith('fwd_')]}")
hit = (pd.to_numeric(sub[label_col], errors="coerce") >= 100).astype(float)

print()
print(f"{'thr':>5} {'fire_n':>8} {'fire_%':>7} {'hit_%':>7} {'avg_fwd15m':>11} {'overall_avg_fwd15m':>19}")
overall_avg = pd.to_numeric(sub[label_col], errors="coerce").mean()
for thr in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
    mask = sub["model_prob"] >= thr
    n = int(mask.sum())
    if n == 0:
        print(f"{thr:5.2f} {n:8d} {'0.0%':>7} {'--':>7} {'--':>11} {overall_avg:19.2f}")
        continue
    fire_pct = 100.0 * n / len(sub)
    hit_pct = 100.0 * hit[mask].mean()
    avg_fwd = pd.to_numeric(sub.loc[mask, label_col], errors="coerce").mean()
    marker = "  <-- LIVE" if abs(thr - LIVE_THRESHOLD) < 1e-6 else ""
    print(f"{thr:5.2f} {n:8d} {fire_pct:6.2f}% {hit_pct:6.1f}% {avg_fwd:11.2f} {overall_avg:19.2f}{marker}")

print()
print(f"base rate (unconditional P(fwd15m>=100pt)): {100*hit.mean():.2f}%")
print(f"prob range: min={sub['model_prob'].min():.4f} max={sub['model_prob'].max():.4f} "
      f"median={sub['model_prob'].median():.4f}")
