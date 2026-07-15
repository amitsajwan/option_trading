#!/usr/bin/env python3
"""Fast, filter-free entry-model-only analysis (2026-07-15, user request):
score the LIVE entry model directly against the (now feature-complete)
training view, with NO regime/IV_FILTER/strategy-router logic at all —
just "at this threshold, how often does the model fire, and when it does,
how often did the labeled move actually happen." The only filter applied
is the model's own feature-completeness gate (max_nan_features), since
evaluating on data the model itself would refuse live is not a fair test.

CORRECTED 2026-07-15: the first version scored the model against its own
training view with no date restriction — since the training view spans the
model's train+valid+holdout windows, that meant >99% of the evaluated rows
were data the model had already seen (BankNifty: train ends 2026-03-31,
valid ends 2026-05-31; NIFTY: train ends 2025-12-31, valid ends 2026-04-30).
Reported hit rates were inflated by memorization, not genuine out-of-sample
skill. This version restricts to ONLY rows strictly after the model's own
valid_window end (from training_metadata), i.e. genuinely never seen by
either training or calibration.

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

print(f"=== {instrument} entry-model-only study (OUT-OF-SAMPLE ONLY) ===", flush=True)
b = joblib.load(MODEL_PATH)
features = b["features"]
medians = b["medians"]
model = b["model"]
max_nan = b.get("max_nan_features", 3)
tm = b.get("training_metadata", {})
valid_end = tm.get("valid_window", "..").split("..")[-1]
print(f"model: {MODEL_PATH}")
print(f"features: {len(features)}  max_nan_features={max_nan}  live_threshold={LIVE_THRESHOLD}")
print(f"train_window={tm.get('train_window')}  valid_window={tm.get('valid_window')}  "
      f"holdout_window={tm.get('holdout_window')}")
print(f"restricting to trade_date > {valid_end} (strictly never seen by training OR calibration)")

df = pd.read_csv(VIEW_PATH, compression="infer")
print(f"training view rows (all dates): {len(df)}")
df = df[df["trade_date"] > valid_end].reset_index(drop=True)
n_days = df["trade_date"].nunique()
print(f"rows after date restriction: {len(df)} across {n_days} genuinely out-of-sample days "
      f"({df['trade_date'].min()} to {df['trade_date'].max()})")

for f in features:
    if f not in df.columns:
        df[f] = np.nan

X_raw = df[features].apply(pd.to_numeric, errors="coerce")
nan_count = X_raw.isna().sum(axis=1)
ok = nan_count <= max_nan
print(f"rows passing feature-completeness (<= {max_nan} NaN of {len(features)}): "
      f"{ok.sum()} / {len(df)} ({100*ok.mean():.1f}%)" if len(df) else "no rows")

X = X_raw.copy()
for f in features:
    X[f] = X[f].fillna(medians.get(f, 0.0))
X = X[ok].reset_index(drop=True)
sub = df[ok].reset_index(drop=True)

if len(sub) == 0:
    print("NO OUT-OF-SAMPLE ROWS AVAILABLE — cannot evaluate.")
    sys.exit(0)

proba = model.predict_proba(X)[:, 1]
sub = sub.assign(model_prob=proba)

label_col = "fwd_maxabs_15m"
hit = (pd.to_numeric(sub[label_col], errors="coerce") >= 100).astype(float)

print()
print(f"{'thr':>5} {'fire_n':>8} {'fire_%':>7} {'hit_%':>7} {'avg_fwd15m':>11}")
overall_avg = pd.to_numeric(sub[label_col], errors="coerce").mean()
for thr in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
    mask = sub["model_prob"] >= thr
    n = int(mask.sum())
    if n == 0:
        print(f"{thr:5.2f} {n:8d} {'0.0%':>7} {'--':>7} {'--':>11}")
        continue
    fire_pct = 100.0 * n / len(sub)
    hit_pct = 100.0 * hit[mask].mean()
    avg_fwd = pd.to_numeric(sub.loc[mask, label_col], errors="coerce").mean()
    marker = "  <-- LIVE" if abs(thr - LIVE_THRESHOLD) < 1e-6 else ""
    print(f"{thr:5.2f} {n:8d} {fire_pct:6.2f}% {hit_pct:6.1f}% {avg_fwd:11.2f}{marker}")

print()
print(f"OUT-OF-SAMPLE base rate (unconditional P(fwd15m>=100pt)): {100*hit.mean():.2f}%  "
      f"(overall avg move: {overall_avg:.2f}pt)")
print(f"prob range: min={sub['model_prob'].min():.4f} max={sub['model_prob'].max():.4f} "
      f"median={sub['model_prob'].median():.4f}")
