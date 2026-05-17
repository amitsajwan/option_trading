"""Walk-forward validation for the option-P&L MVP trainer.

Gates the rest of the experiment: if the MVP-style edge (positive holdout
net P&L) holds across multiple rolling windows, the signal is structural,
not a single-window artifact. If it only shows on one window, it was
selection bias.

Design:
  - Anchor on the existing C1 train/valid/holdout windowing for the last
    window so we can compare to MVP result directly.
  - For each window, train per-recipe XGBoost with sensible defaults
    (same as MVP), threshold-sweep on the window's holdout, record best
    net_pnl_sum, n_trades, win_rate, AUC.
  - Verdict per recipe:
      * EDGE_ROBUST: best_net_pnl > 0 on >= 75% of windows
      * EDGE_PARTIAL: 50-75% of windows positive
      * NO_EDGE: < 50% positive (likely the MVP edge was a single-window
        artifact)

Windows used (expanding train, 3-month rolling holdout, no valid overlap):
  W1: train 2020-08 → 2022-04 (21mo) | holdout 2022-05 → 2022-07
  W2: train 2020-08 → 2022-07 (24mo) | holdout 2022-08 → 2022-10
  W3: train 2020-08 → 2022-10 (27mo) | holdout 2022-11 → 2023-01
  W4: train 2020-08 → 2023-01 (30mo) | holdout 2023-02 → 2023-04
  W5: train 2020-08 → 2023-04 (33mo) | holdout 2023-05 → 2023-07
  W6: train 2020-08 → 2023-07 (36mo) | holdout 2023-08 → 2023-10
  W7: train 2020-08 → 2023-10 (39mo) | holdout 2023-11 → 2024-01
  W8: train 2020-08 → 2024-01 (42mo) | holdout 2024-02 → 2024-04
  W9: train 2020-08 → 2024-04 (45mo) | holdout 2024-05 → 2024-07
  W10: train 2020-08 → 2024-07 (48mo) | holdout 2024-08 → 2024-10  (== MVP holdout)

If only W10 is positive, the MVP "edge" was specific to Aug-Oct 2024.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    import xgboost as xgb
    from sklearn.metrics import roc_auc_score
except ImportError as exc:
    print(f"ERROR: missing dependency: {exc}", file=sys.stderr)
    sys.exit(2)

from ml_pipeline_2.scripts.train_option_pnl_mvp import (
    DEFAULT_FLAT_ROOT,
    DEFAULT_LABELS_ROOT,
    load_labels_and_features,
    select_feature_columns,
)


WINDOWS = [
    ("W1", "2020-08-01", "2022-04-30", "2022-05-01", "2022-07-31"),
    ("W2", "2020-08-01", "2022-07-31", "2022-08-01", "2022-10-31"),
    ("W3", "2020-08-01", "2022-10-31", "2022-11-01", "2023-01-31"),
    ("W4", "2020-08-01", "2023-01-31", "2023-02-01", "2023-04-30"),
    ("W5", "2020-08-01", "2023-04-30", "2023-05-01", "2023-07-31"),
    ("W6", "2020-08-01", "2023-07-31", "2023-08-01", "2023-10-31"),
    ("W7", "2020-08-01", "2023-10-31", "2023-11-01", "2024-01-31"),
    ("W8", "2020-08-01", "2024-01-31", "2024-02-01", "2024-04-30"),
    ("W9", "2020-08-01", "2024-04-30", "2024-05-01", "2024-07-31"),
    ("W10", "2020-08-01", "2024-07-31", "2024-08-01", "2024-10-31"),
]

# Threshold sweep — same as MVP for apples-to-apples comparison
THRESHOLDS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]


@dataclass
class WindowResult:
    window_id: str
    train_start: str
    train_end: str
    holdout_start: str
    holdout_end: str
    n_train: int
    n_holdout: int
    holdout_pos_rate: float
    holdout_auc: float
    best_threshold: float
    best_net_pnl_sum: float
    best_n_trades: int
    best_win_rate: float
    threshold_sweep: list[dict]


def train_one_window(
    df: pd.DataFrame,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    holdout_start: pd.Timestamp,
    holdout_end: pd.Timestamp,
) -> Optional[WindowResult]:
    feat_cols = select_feature_columns(df)
    train_mask = (df["trade_date"] >= train_start) & (df["trade_date"] <= train_end)
    holdout_mask = (df["trade_date"] >= holdout_start) & (df["trade_date"] <= holdout_end)
    train = df[train_mask]
    holdout = df[holdout_mask]
    if len(train) < 1000 or len(holdout) < 200:
        return None

    X_tr = train[feat_cols].fillna(0.0).to_numpy(dtype=np.float32)
    y_tr = train["label"].astype(int).to_numpy()
    X_ho = holdout[feat_cols].fillna(0.0).to_numpy(dtype=np.float32)
    y_ho = holdout["label"].astype(int).to_numpy()
    pnl_ho = holdout["net_pnl_pct"].astype(float).to_numpy()

    model = xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.85, colsample_bytree=0.85, reg_lambda=2.0,
        objective="binary:logistic", eval_metric="auc",
        tree_method="hist", n_jobs=4, random_state=42,
    )
    model.fit(X_tr, y_tr, verbose=False)
    p_ho = model.predict_proba(X_ho)[:, 1]

    try:
        auc = float(roc_auc_score(y_ho, p_ho))
    except Exception:
        auc = float("nan")

    sweep: list[dict] = []
    for thr in THRESHOLDS:
        mask = p_ho >= thr
        n_trades = int(mask.sum())
        if n_trades == 0:
            sweep.append({"threshold": thr, "n_trades": 0, "net_pnl_sum": 0.0, "win_rate": 0.0})
            continue
        traded = pnl_ho[mask]
        sweep.append({
            "threshold": thr,
            "n_trades": n_trades,
            "net_pnl_sum": float(traded.sum()),
            "win_rate": float((traded > 0).mean()),
        })
    best = max(sweep, key=lambda d: d["net_pnl_sum"])

    return WindowResult(
        window_id="",  # set by caller
        train_start=str(train_start.date()),
        train_end=str(train_end.date()),
        holdout_start=str(holdout_start.date()),
        holdout_end=str(holdout_end.date()),
        n_train=len(train),
        n_holdout=len(holdout),
        holdout_pos_rate=float(y_ho.mean()),
        holdout_auc=auc,
        best_threshold=float(best["threshold"]),
        best_net_pnl_sum=float(best["net_pnl_sum"]),
        best_n_trades=int(best.get("n_trades", 0)),
        best_win_rate=float(best.get("win_rate", 0.0)),
        threshold_sweep=sweep,
    )


def verdict(positive_count: int, total: int) -> str:
    rate = positive_count / total if total else 0.0
    if rate >= 0.75:
        return "EDGE_ROBUST"
    if rate >= 0.50:
        return "EDGE_PARTIAL"
    return "NO_EDGE"


def run(args) -> int:
    labels_root = Path(args.labels)
    flat_root = Path(args.flat)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    recipe_ids = args.recipes or ["ATM_CE_9", "ATM_PE_9", "ATM_CE_15", "ATM_PE_15"]
    print("=== Walk-forward validation ===")
    print(f"labels: {labels_root}")
    print(f"recipes: {recipe_ids}")
    print(f"windows: {len(WINDOWS)}")
    print()

    all_results: dict = {}
    for recipe_id in recipe_ids:
        print(f"--- {recipe_id} ---")
        try:
            df = load_labels_and_features(labels_root, flat_root, recipe_id)
        except Exception as exc:
            print(f"  ERROR loading: {exc}", file=sys.stderr)
            all_results[recipe_id] = {"error": str(exc)}
            continue

        window_results: list[dict] = []
        positive_count = 0
        for w_id, t0, t1, h0, h1 in WINDOWS:
            res = train_one_window(
                df,
                pd.Timestamp(t0), pd.Timestamp(t1),
                pd.Timestamp(h0), pd.Timestamp(h1),
            )
            if res is None:
                print(f"  {w_id}: skipped (insufficient rows)")
                continue
            res.window_id = w_id
            window_results.append(res.__dict__)
            is_positive = res.best_net_pnl_sum > 0
            if is_positive:
                positive_count += 1
            print(f"  {w_id} | hold {res.holdout_start[:7]}-{res.holdout_end[:7]} | "
                  f"AUC={res.holdout_auc:.3f} | best_thr={res.best_threshold:.2f} | "
                  f"trades={res.best_n_trades:4d} | net={res.best_net_pnl_sum:+7.2f} | "
                  f"wr={res.best_win_rate:.3f} | {'POS' if is_positive else 'neg'}")
        v = verdict(positive_count, len(window_results))
        print(f"  → {positive_count}/{len(window_results)} windows positive → {v}")
        all_results[recipe_id] = {
            "verdict": v,
            "positive_windows": positive_count,
            "total_windows": len(window_results),
            "windows": window_results,
        }
        print()

    print("=== Aggregate ===")
    for rid in recipe_ids:
        r = all_results.get(rid, {})
        print(f"  {rid}: {r.get('verdict')} ({r.get('positive_windows')}/{r.get('total_windows')})")

    (out_dir / "walk_forward_results.json").write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nwrote: {out_dir / 'walk_forward_results.json'}")

    robust = [rid for rid, r in all_results.items() if r.get("verdict") == "EDGE_ROBUST"]
    if robust:
        print(f"\nEDGE_ROBUST recipes: {robust}  ← greenlight HPO + grid")
        return 0
    partial = [rid for rid, r in all_results.items() if r.get("verdict") == "EDGE_PARTIAL"]
    if partial:
        print(f"\nEDGE_PARTIAL recipes: {partial}  ← worth HPO if compute is cheap; treat as weak signal")
        return 0
    print("\nNO_EDGE on all recipes  ← MVP result was likely single-window artifact")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Walk-forward validation for option-P&L recipes")
    p.add_argument("--labels", default=str(DEFAULT_LABELS_ROOT))
    p.add_argument("--flat", default=str(DEFAULT_FLAT_ROOT))
    p.add_argument("--out", required=True)
    p.add_argument("--recipes", nargs="*", default=None)
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
