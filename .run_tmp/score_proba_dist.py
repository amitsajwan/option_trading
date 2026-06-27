"""Check actual probability distribution and compute metrics at quantile thresholds."""
import sys, os
sys.path.insert(0, os.path.expanduser("~/bmm_run/ml_pipeline_2/src"))
sys.path.insert(0, os.path.expanduser("~/bmm_run"))

import numpy as np
import pandas as pd
import joblib, duckdb
from sklearn.metrics import roc_auc_score, precision_score, recall_score
from ml_pipeline_2.staged.entry_move_oracle import build_entry_bn_move_oracle
from pathlib import Path

VIEW_ROOT    = "/home/amits/parquet_data/stage1_entry_view_v2"
SUPPORT_ROOT = "/home/amits/parquet_data/snapshots_ml_flat_v2"
START, END   = "2026-06-01", "2026-06-17"
ARTIFACTS    = Path("/home/amits/bmm_run/ml_pipeline_2/artifacts/research")
DAYS         = 8


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


def analyze(name, y, proba):
    auc = roc_auc_score(y, proba)
    n_total = len(y)
    n_pos = y.sum()

    print(f"\n{'='*68}")
    print(f"  {name}  |  AUC={auc:.4f}  |  n={n_total}  |  true_moves={n_pos} ({n_pos/n_total*100:.1f}%)")
    print(f"{'='*68}")

    # Probability distribution
    pcts = [50, 70, 80, 85, 90, 95, 99]
    print(f"  Probability distribution (all {n_total} bars):")
    print(f"    min={proba.min():.4f}  mean={proba.mean():.4f}  max={proba.max():.4f}")
    print(f"    percentiles: " + "  ".join(f"p{p}={np.percentile(proba,p):.4f}" for p in pcts))

    # Among true-move bars
    p_pos = proba[y == 1]
    p_neg = proba[y == 0]
    print(f"  Prob on true-move bars (n={len(p_pos)}): mean={p_pos.mean():.4f}  p50={np.median(p_pos):.4f}  p90={np.percentile(p_pos,90):.4f}")
    print(f"  Prob on no-move  bars (n={len(p_neg)}): mean={p_neg.mean():.4f}  p50={np.median(p_neg):.4f}  p90={np.percentile(p_neg,90):.4f}")

    # Use quantile-based thresholds (top-X% of predictions as signals)
    print(f"\n  Top-decile / quantile threshold table:")
    print(f"  {'top%':>5}  {'thresh':>7}  {'signals':>8}  {'sig/day':>8}  {'precision':>10}  {'recall':>8}  {'hits':>6}")
    print(f"  {'-'*5}  {'-'*7}  {'-'*8}  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*6}")
    for pct in [5, 10, 15, 20, 25, 30]:
        t = np.percentile(proba, 100 - pct)
        pred = (proba >= t).astype(int)
        sigs = pred.sum()
        if sigs == 0:
            continue
        prec = precision_score(y, pred, zero_division=0)
        rec  = recall_score(y, pred, zero_division=0)
        hits = int(round(prec * sigs))
        print(f"  {pct:>5}%  {t:>7.4f}  {sigs:>8}  {sigs/DAYS:>8.1f}  {prec:>10.3f}  {rec:>8.3f}  {hits:>6}")

    # What threshold does live use (ENTRY_ML_MIN_PROB=0.65)?
    # But also what quantile does 0.65 correspond to here?
    live_t = 0.65
    live_pct = (proba < live_t).mean() * 100
    print(f"\n  Live threshold 0.65 = p{live_pct:.0f} of this distribution → 0 signals (prob ceiling={proba.max():.4f})")


# ── load ─────────────────────────────────────────────────────────────────────
view    = load_window(VIEW_ROOT, START, END)
support = load_window(SUPPORT_ROOT, START, END,
                      cols=["trade_date","timestamp","snapshot_id",
                            "px_fut_open","px_fut_high","px_fut_low","px_fut_close"])
oracle  = build_entry_bn_move_oracle(support, horizon_minutes=5, min_pct=0.002, side="any")
label   = oracle[["trade_date","timestamp","snapshot_id","entry_label","entry_label_valid"]]
merged  = view.merge(label, on=["trade_date","timestamp","snapshot_id"], how="inner")
merged  = merged[pd.to_numeric(merged["entry_label_valid"], errors="coerce").fillna(0) == 1].copy()
y       = pd.to_numeric(merged["entry_label"], errors="coerce").fillna(0).astype(int).to_numpy()

for name, pat in [
    ("bmm_prod",      "bmm_prod_5m020_v2view_*/stages/stage1/model.joblib"),
    ("velocity_base", "ab_5m020_base_*/stages/stage1/model.joblib"),
]:
    paths = sorted(ARTIFACTS.glob(pat))
    if not paths: print(f"[{name}] not found"); continue
    est, feats = load_bundle(paths[0])
    X = merged.reindex(columns=feats)
    for c in X.columns:
        X[c] = pd.to_numeric(X[c], errors="coerce")
    proba = est.predict_proba(X)[:, 1]
    analyze(name, y, proba)
