"""Score entry models and compute per-threshold metrics: signal count, precision, recall."""
import sys, os
sys.path.insert(0, os.path.expanduser("~/bmm_run/ml_pipeline_2/src"))
sys.path.insert(0, os.path.expanduser("~/bmm_run"))

import numpy as np
import pandas as pd
import joblib
import duckdb
from sklearn.metrics import roc_auc_score, precision_score, recall_score
from ml_pipeline_2.staged.entry_move_oracle import build_entry_bn_move_oracle
from pathlib import Path

VIEW_ROOT    = "/home/amits/parquet_data/stage1_entry_view_v2"
SUPPORT_ROOT = "/home/amits/parquet_data/snapshots_ml_flat_v2"
START, END   = "2026-06-01", "2026-06-17"
THRESHOLDS   = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]

ARTIFACTS = Path("/home/amits/bmm_run/ml_pipeline_2/artifacts/research")


def load_window(root, start, end, cols=None):
    g = os.path.join(root, "**", "*.parquet")
    sel = "*" if cols is None else ", ".join(f'"{c}"' for c in cols)
    con = duckdb.connect(":memory:")
    df = con.execute(
        f"SELECT {sel} FROM read_parquet('{g}', hive_partitioning=false, union_by_name=true)"
        f" WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date, timestamp",
        [start, end],
    ).df()
    con.close()
    return df


def load_bundle(path):
    b = joblib.load(path)
    if isinstance(b, dict):
        feats = b.get("feature_columns")
        if feats is None:
            c = b.get("_model_input_contract") or {}
            feats = c.get("feature_columns") or c.get("features")
        models = b.get("models")
        est = models.get("move") if isinstance(models, dict) else models
    else:
        est, feats = b, None
    return est, list(feats) if feats else None


def threshold_table(y, proba, name):
    auc = roc_auc_score(y, proba)
    n_pos = y.sum()
    n_total = len(y)
    days = 8
    print(f"\n{'='*60}")
    print(f"  {name}  |  AUC={auc:.4f}  |  n={n_total}  |  true_moves={n_pos} ({n_pos/n_total*100:.1f}%)")
    print(f"{'='*60}")
    print(f"  {'thresh':>6}  {'signals':>8}  {'sig/day':>8}  {'precision':>10}  {'recall':>8}  {'hits':>6}")
    print(f"  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*6}")
    for t in THRESHOLDS:
        pred = (proba >= t).astype(int)
        sigs = pred.sum()
        if sigs == 0:
            print(f"  {t:>6.2f}  {sigs:>8}  {'0.0':>8}  {'—':>10}  {'—':>8}  {'—':>6}")
            continue
        prec = precision_score(y, pred, zero_division=0)
        rec  = recall_score(y, pred, zero_division=0)
        hits = int(prec * sigs)
        print(f"  {t:>6.2f}  {sigs:>8}  {sigs/days:>8.1f}  {prec:>10.3f}  {rec:>8.3f}  {hits:>6}")


# ── load data ────────────────────────────────────────────────────────────────
print("Loading view and support...")
view    = load_window(VIEW_ROOT, START, END)
support = load_window(SUPPORT_ROOT, START, END,
                      cols=["trade_date","timestamp","snapshot_id",
                            "px_fut_open","px_fut_high","px_fut_low","px_fut_close"])

oracle = build_entry_bn_move_oracle(support, horizon_minutes=5, min_pct=0.002, side="any")
label  = oracle[["trade_date","timestamp","snapshot_id","entry_label","entry_label_valid"]]

merged = view.merge(label, on=["trade_date","timestamp","snapshot_id"], how="inner")
merged = merged[pd.to_numeric(merged["entry_label_valid"], errors="coerce").fillna(0) == 1].copy()
y = pd.to_numeric(merged["entry_label"], errors="coerce").fillna(0).astype(int).to_numpy()
print(f"Labeled rows={len(merged)}, positives={y.sum()} ({y.mean()*100:.1f}%)")

# ── score bundles ─────────────────────────────────────────────────────────────
bundles = [
    ("bmm_prod",      sorted(ARTIFACTS.glob("bmm_prod_5m020_v2view_*/stages/stage1/model.joblib"))),
    ("velocity_base", sorted(ARTIFACTS.glob("ab_5m020_base_*/stages/stage1/model.joblib"))),
]

for name, paths in bundles:
    if not paths:
        print(f"[{name}] not found"); continue
    est, feats = load_bundle(paths[0])
    X = merged.reindex(columns=feats)
    for c in X.columns:
        X[c] = pd.to_numeric(X[c], errors="coerce")
    proba = est.predict_proba(X)[:, 1]
    threshold_table(y, proba, name)
