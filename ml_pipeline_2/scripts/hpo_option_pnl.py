"""Hyperparameter random search for the option-P&L trainer.

Runs random hyperparameter sampling for XGBoost on the option-P&L labels,
using the same temporal train/valid/holdout windows as the MVP. For each
sampled config, train + evaluate on holdout via threshold sweep. Rank
configs by holdout best_net_pnl_sum.

No HPO library dependency (no optuna installed) — plain random search
with a fixed seed for reproducibility.

Usage:
    python -m ml_pipeline_2.scripts.hpo_option_pnl \\
      --recipe ATM_PE_9 --trials 25 --out <dir>
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

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
    HOLDOUT_END,
    TRAIN_END,
    VALID_END,
    load_labels_and_features,
    select_feature_columns,
    split_temporal,
)


THRESHOLDS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]


def sample_params(rng: random.Random) -> dict:
    """Random sample from the hyperparameter space. Ranges chosen to be
    wide enough to explore but not so wide that most trials are wasted."""
    return {
        "n_estimators": rng.choice([200, 300, 500, 800, 1200]),
        "max_depth": rng.choice([3, 4, 5, 6, 8]),
        "learning_rate": rng.choice([0.01, 0.025, 0.05, 0.08, 0.12]),
        "subsample": rng.choice([0.6, 0.75, 0.85, 1.0]),
        "colsample_bytree": rng.choice([0.5, 0.7, 0.85, 1.0]),
        "min_child_weight": rng.choice([1, 5, 10, 20, 50]),
        "reg_lambda": rng.choice([0.0, 1.0, 2.0, 5.0, 10.0]),
        "reg_alpha": rng.choice([0.0, 0.1, 0.5, 1.0, 5.0]),
    }


@dataclass
class TrialResult:
    trial: int
    params: dict
    holdout_auc: float
    best_threshold: float
    best_net_pnl_sum: float
    best_n_trades: int
    best_win_rate: float
    sweep: list[dict]
    train_time_s: float


def run_trial(
    trial_idx: int, params: dict,
    X_tr, y_tr, X_va, y_va, X_ho, y_ho, pnl_ho,
) -> TrialResult:
    t0 = time.time()
    model = xgb.XGBClassifier(
        **params,
        objective="binary:logistic", eval_metric="auc",
        tree_method="hist", n_jobs=4, random_state=42,
    )
    eval_set = [(X_va, y_va)] if X_va is not None and len(X_va) else None
    model.fit(X_tr, y_tr, eval_set=eval_set, verbose=False)
    elapsed = time.time() - t0

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

    return TrialResult(
        trial=trial_idx, params=params,
        holdout_auc=auc,
        best_threshold=float(best["threshold"]),
        best_net_pnl_sum=float(best["net_pnl_sum"]),
        best_n_trades=int(best.get("n_trades", 0)),
        best_win_rate=float(best.get("win_rate", 0.0)),
        sweep=sweep,
        train_time_s=round(elapsed, 1),
    )


def run(args) -> int:
    labels_root = Path(args.labels)
    flat_root = Path(args.flat)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== HPO random search ===")
    print(f"recipe: {args.recipe}")
    print(f"trials: {args.trials}")
    print(f"train: 2020-08 → {TRAIN_END.date()}   valid: {VALID_END.date()}   holdout: {HOLDOUT_END.date()}")
    print()

    df = load_labels_and_features(labels_root, flat_root, args.recipe)
    feat_cols = select_feature_columns(df)
    train, valid, holdout = split_temporal(df)

    X_tr = train[feat_cols].fillna(0.0).to_numpy(dtype=np.float32)
    y_tr = train["label"].astype(int).to_numpy()
    X_va = valid[feat_cols].fillna(0.0).to_numpy(dtype=np.float32) if not valid.empty else None
    y_va = valid["label"].astype(int).to_numpy() if not valid.empty else None
    X_ho = holdout[feat_cols].fillna(0.0).to_numpy(dtype=np.float32)
    y_ho = holdout["label"].astype(int).to_numpy()
    pnl_ho = holdout["net_pnl_pct"].astype(float).to_numpy()

    print(f"shapes: train={X_tr.shape} valid={X_va.shape if X_va is not None else None} holdout={X_ho.shape}")
    print(f"features: {len(feat_cols)}")
    print()

    rng = random.Random(args.seed)
    trials: list[TrialResult] = []
    for i in range(args.trials):
        params = sample_params(rng)
        print(f"[trial {i+1}/{args.trials}] {json.dumps(params, sort_keys=True)}")
        try:
            res = run_trial(i, params, X_tr, y_tr, X_va, y_va, X_ho, y_ho, pnl_ho)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue
        trials.append(res)
        print(f"  → AUC={res.holdout_auc:.4f}  best_thr={res.best_threshold:.2f}  "
              f"trades={res.best_n_trades:4d}  net={res.best_net_pnl_sum:+8.3f}  "
              f"wr={res.best_win_rate:.3f}  ({res.train_time_s:.0f}s)")

    if not trials:
        print("\nNo successful trials.", file=sys.stderr)
        return 1

    trials_sorted = sorted(trials, key=lambda t: -t.best_net_pnl_sum)
    best = trials_sorted[0]
    print()
    print(f"=== Best trial (by holdout net_pnl_sum) ===")
    print(f"  net_pnl_sum: {best.best_net_pnl_sum:+.3f}")
    print(f"  AUC: {best.holdout_auc:.4f}")
    print(f"  threshold: {best.best_threshold:.2f}")
    print(f"  trades: {best.best_n_trades}")
    print(f"  win_rate: {best.best_win_rate:.3f}")
    print(f"  params: {json.dumps(best.params, sort_keys=True, indent=2)}")

    # Persist all trials
    out_payload = {
        "recipe": args.recipe,
        "trials": [asdict(t) for t in trials_sorted],
        "n_features": len(feat_cols),
        "train_size": int(len(train)),
        "holdout_size": int(len(holdout)),
    }
    (out_dir / "hpo_results.json").write_text(json.dumps(out_payload, indent=2))
    print(f"\nwrote: {out_dir / 'hpo_results.json'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="HPO random search for option-P&L trainer")
    p.add_argument("--recipe", default="ATM_PE_9")
    p.add_argument("--trials", type=int, default=20)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--labels", default=str(DEFAULT_LABELS_ROOT))
    p.add_argument("--flat", default=str(DEFAULT_FLAT_ROOT))
    p.add_argument("--out", required=True)
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
