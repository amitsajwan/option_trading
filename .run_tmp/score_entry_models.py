"""Score entry models (v3-velocity vs bmm-compression) on a stage1_entry_view_v2
dataset, using the entry_bn 5m/0.20% move label built from a support flat dataset.

Phase A (validation): point at the existing training parquet 2024-08..10 holdout and
confirm we reproduce the documented AUCs (bmm_prod 0.8146, velocity ~0.831).
Phase B (forward): point at the June-2026 rebuilt view + support.

Usage:
  python3 score_entry_models.py --view-root <dir> --support-root <dir> \
      --start 2024-08-01 --end 2024-10-31 \
      --bundle bmm_prod=/path/model.joblib --bundle velocity=/path/model.joblib
"""
import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd
import joblib
from sklearn.metrics import roc_auc_score

sys.path.insert(0, os.path.expanduser("~/bmm_run/ml_pipeline_2/src"))
sys.path.insert(0, os.path.expanduser("~/bmm_run"))

from ml_pipeline_2.staged.entry_move_oracle import build_entry_bn_move_oracle


def _glob(root):
    return os.path.join(root, "**", "*.parquet")


def load_window(root, start, end, cols=None):
    import duckdb
    g = _glob(root)
    sel = "*" if cols is None else ", ".join(f'"{c}"' for c in cols)
    rp = f"read_parquet('{g}', hive_partitioning=false, union_by_name=true)"
    con = duckdb.connect(":memory:")
    df = con.execute(
        f"SELECT {sel} FROM {rp} WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date, timestamp",
        [start, end],
    ).df()
    con.close()
    return df


def build_label(support_df):
    """5m / 0.20% magnitude move label (entry_bn_5m_100pts_v1 with min_pct=0.002)."""
    support = support_df.copy()
    # entry_move_oracle expects KEY_COLUMNS + fut_close (mapped from px_fut_*)
    keep = ["trade_date", "timestamp", "snapshot_id", "px_fut_open", "px_fut_high",
            "px_fut_low", "px_fut_close"]
    support = support[[c for c in keep if c in support.columns]].copy()
    support = support.drop_duplicates(subset=["trade_date", "timestamp", "snapshot_id"])
    oracle = build_entry_bn_move_oracle(
        support, horizon_minutes=5, min_pct=0.002, side="any",
    )
    return oracle[["trade_date", "timestamp", "snapshot_id", "entry_label", "entry_label_valid"]]


def load_bundle(path):
    b = joblib.load(path)
    feats = None
    if isinstance(b, dict):
        feats = b.get("feature_columns")
        if feats is None:
            c = b.get("_model_input_contract") or {}
            feats = c.get("feature_columns") or c.get("features")
        models = b.get("models")
        est = None
        if isinstance(models, dict):
            est = models.get("move") or next(iter(models.values()))
        else:
            est = models
    else:
        est, feats = b, None
    return est, list(feats) if feats is not None else None


def score(est, feats, view_df):
    X = view_df.reindex(columns=feats)
    for c in X.columns:
        X[c] = pd.to_numeric(X[c], errors="coerce")
    proba = est.predict_proba(X)[:, 1]
    return proba


def bootstrap_ci(y, p, n=2000, seed=42):
    rng = np.random.default_rng(seed)
    idx = np.arange(len(y))
    aucs = []
    for _ in range(n):
        s = rng.choice(idx, size=len(idx), replace=True)
        ys = y[s]
        if ys.min() == ys.max():
            continue
        aucs.append(roc_auc_score(ys, p[s]))
    if not aucs:
        return (float("nan"), float("nan"))
    return (float(np.percentile(aucs, 2.5)), float(np.percentile(aucs, 97.5)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--view-root", required=True)
    ap.add_argument("--support-root", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--bundle", action="append", default=[], help="name=path")
    a = ap.parse_args()

    print(f"window={a.start}..{a.end}")
    view = load_window(a.view_root, a.start, a.end)
    support = load_window(a.support_root, a.start, a.end,
                          cols=["trade_date", "timestamp", "snapshot_id",
                                "px_fut_open", "px_fut_high", "px_fut_low", "px_fut_close"])
    print(f"view_rows={len(view)} support_rows={len(support)} "
          f"days={view['trade_date'].nunique()}")

    label = build_label(support)
    merged = view.merge(label, on=["trade_date", "timestamp", "snapshot_id"], how="inner")
    merged = merged[pd.to_numeric(merged["entry_label_valid"], errors="coerce").fillna(0) == 1].copy()
    y = pd.to_numeric(merged["entry_label"], errors="coerce").fillna(0).astype(int).to_numpy()
    print(f"labeled_rows={len(merged)} positive_rate={y.mean():.4f}")

    for spec in a.bundle:
        name, path = spec.split("=", 1)
        est, feats = load_bundle(path)
        if est is None:
            print(f"[{name}] could not load estimator from {path}")
            continue
        missing = [f for f in feats if f not in merged.columns]
        if missing:
            print(f"[{name}] WARNING missing {len(missing)} feats e.g. {missing[:6]}")
        p = score(est, feats, merged)
        auc = roc_auc_score(y, p)
        lo, hi = bootstrap_ci(y, p)
        print(f"[{name}] n_feats={len(feats)} AUC={auc:.4f}  95%CI=[{lo:.4f},{hi:.4f}]  n={len(y)}")


if __name__ == "__main__":
    main()
