"""Train a regression model against option_payoff_labels.py's cost-aware,
direction-agnostic payoff score -- a genuinely different training shape from
train_entry_dhan_v3.py (regression, not classification), so it's its own
script per this repo's established convention (train_entry_rarelabel_v1.py /
train_entry_v3label_mongo.py already coexist as siblings for the same
reason: different label shapes, not different flags on one script).

Input is the OUTPUT of build_option_pnl_training_view.py -- a training-view
CSV with the usual feature columns PLUS payoff_score/payoff_winning_side/
payoff_strike joined on. Same feature set as train_entry_dhan_v3.py
(ENTRY_FEATURES_V3), imported from there rather than redefined, so both
models are trained on an apples-to-apples feature basis.

What "ship gate" means for a regression target: AUC/separation-table don't
apply. What actually matters for a graded, "how good was this entry"
score is RANKING skill -- does sorting bars by predicted payoff actually
put the genuinely profitable ones at the top -- so the gates here are a
decile-lift table (mean ACTUAL payoff per PREDICTED-score decile on
holdout) and Spearman rank correlation, not R²/MSE (which would reward
getting the exact magnitude right on unprofitable bars just as much as
profitable ones -- not what a "should we trade this" decision needs).

Usage:
    python -m ml_pipeline_2.scripts.train_entry_option_payoff_v1 \\
        --training-view-csv .run/training_view/training_view_nifty_option_pnl.csv.gz \\
        --output models/nifty_entry_option_payoff_v1.joblib \\
        --train-start 2024-11-04 --train-end 2026-04-30 \\
        --valid-start 2026-05-01 --valid-end 2026-05-31 \\
        --holdout-start 2026-06-01 --holdout-end 2026-07-31

Before trusting this model's holdout numbers: run
ml_pipeline_2.scripts.run_repricing_wall_control against its holdout fires
(top-decile bars) -- a positive decile-lift here is necessary but NOT
sufficient; this project has three prior documented incidents of an
apparent entry-timing edge evaporating the instant entry was delayed 1-2
bars past the model's own moment of confidence.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("train_entry_option_payoff_v1")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3  # noqa: E402

TARGET_COL = "payoff_score"
N_DECILES = 10

# Regression analog of train_entry_dhan_v3's ship gates -- translated, not
# copied, per this study's own evaluation-protocol decision (AUC doesn't
# apply to a continuous target).
SHIP_GATE_TOP_DECILE_MEAN_MIN = 0.0       # top-predicted-decile must be net profitable, not just "highest of a bad bunch"
SHIP_GATE_SPEARMAN_MIN = 0.05             # weak-but-real linear rank skill; near-zero = no ranking ability at all
SHIP_GATE_SPEARMAN_P_MAX = 0.05           # must be statistically distinguishable from no correlation
SHIP_GATE_MIN_HOLDOUT_ROWS = 200          # decile analysis on fewer than this is not trustworthy (20/decile)


def winsorize_target(y: np.ndarray, *, lower_pct: float = 0.01, upper_pct: float = 0.99) -> tuple[np.ndarray, float, float]:
    """Clip extreme tail values in the TRAINING target to the
    [lower_pct, upper_pct] percentile range before fitting a
    squared-error regressor.

    Real 2026-09-26 run against full NIFTY history: payoff_score ranges
    from -1.11 to +11.80 (a handful of near-expiry, near-floor-premium
    entries where a small fixed brokerage fee is a huge percentage of a
    tiny entry value, or a rare large tail move), while the bulk of the
    distribution sits in a much tighter band (25th/50th/75th percentiles
    0.011/0.050/0.123). A squared-error loss weights a +1180% row 10,000x
    more than a +5% row -- a handful of these can dominate the whole fit
    and pull every prediction toward chasing rare tail rows instead of
    learning the much more common, more decision-relevant mid-range
    signal. Only ever apply this to the TRAINING target -- validation and
    holdout targets must stay real/unclipped so reported ship-gate numbers
    reflect actual, un-doctored economics.
    """
    lo = float(np.quantile(y, lower_pct))
    hi = float(np.quantile(y, upper_pct))
    return np.clip(y, lo, hi), lo, hi


def load_training_view(path: str, *, start: str, end: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    if TARGET_COL not in df.columns:
        raise ValueError(
            f"{path} has no '{TARGET_COL}' column -- did you run "
            f"build_option_pnl_training_view.py on it first?"
        )
    df = df[(df["trade_date"] >= start) & (df["trade_date"] <= end)]
    return df


def _select_features(df: pd.DataFrame, features: List[str]) -> pd.DataFrame:
    missing = [f for f in features if f not in df.columns]
    if missing:
        raise ValueError(f"training view is missing expected feature columns: {missing}")
    return df[features].apply(pd.to_numeric, errors="coerce")


def run_hpo(
    X_train: pd.DataFrame, y_train: np.ndarray,
    X_valid: pd.DataFrame, y_valid: np.ndarray,
    n_trials: int = 50,
) -> tuple[Any, Dict[str, Any], float]:
    """Optuna HPO over XGBRegressor hyperparameters, optimizing validation
    Spearman rank correlation (not RMSE) -- ranking skill is what a
    "should we trade this bar" decision actually needs; a model that gets
    the exact magnitude wrong but the ORDERING right is still useful, and
    one that minimizes RMSE by being conservative everywhere may rank
    nothing usefully."""
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        import xgboost as xgb
        from scipy.stats import spearmanr
    except ImportError as exc:
        raise ImportError(f"Install optuna + xgboost + scipy: pip install optuna xgboost scipy — {exc}")

    def objective(trial):
        params = {
            "n_estimators":     trial.suggest_int("n_estimators", 200, 1000),
            "max_depth":        trial.suggest_int("max_depth", 3, 8),
            "learning_rate":    trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "subsample":        trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 20),
            "reg_alpha":        trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
            "reg_lambda":       trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
            "objective":        "reg:squarederror",
            "random_state":     42,
            "n_jobs":           -1,
        }
        model = xgb.XGBRegressor(**params)
        model.fit(X_train, y_train, verbose=False)
        pred = model.predict(X_valid)
        rho, _ = spearmanr(pred, y_valid)
        return 0.0 if np.isnan(rho) else float(rho)

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    log.info("Best validation Spearman rho=%.4f params=%s", study.best_value, study.best_params)

    import xgboost as xgb
    best = dict(study.best_params)
    best["objective"] = "reg:squarederror"
    best["random_state"] = 42
    best["n_jobs"] = -1
    final = xgb.XGBRegressor(**best)
    final.fit(X_train, y_train, verbose=False)
    return final, study.best_params, study.best_value


def decile_lift_table(predicted: np.ndarray, actual: np.ndarray, *, n_deciles: int = N_DECILES) -> List[Dict[str, Any]]:
    """Sort holdout rows by PREDICTED payoff into `n_deciles` equal-sized
    buckets (10 = highest predicted, 1 = lowest), report each bucket's mean
    ACTUAL payoff, win rate (actual > 0), and row count. This is the
    regression analog of train_entry_dhan_v3's separation_table -- the
    actual "would picking this model's top-ranked bars have worked" check,
    not a generic goodness-of-fit statistic."""
    order = np.argsort(predicted)
    actual_sorted = actual[order]
    n = len(actual_sorted)
    rows = []
    bucket_edges = np.linspace(0, n, n_deciles + 1).astype(int)
    for i in range(n_deciles):
        lo, hi = bucket_edges[i], bucket_edges[i + 1]
        bucket = actual_sorted[lo:hi]
        if len(bucket) == 0:
            rows.append({"decile": i + 1, "n": 0, "mean_actual_payoff": None, "win_rate": None})
            continue
        rows.append({
            "decile": i + 1,  # 1 = lowest predicted, n_deciles = highest predicted
            "n": int(len(bucket)),
            "mean_actual_payoff": round(float(bucket.mean()), 5),
            "win_rate": round(float((bucket > 0).mean()), 4),
        })
    return rows


def evaluate_regression(model: Any, X: pd.DataFrame, y: np.ndarray) -> Dict[str, Any]:
    from scipy.stats import spearmanr

    pred = model.predict(X)
    rho, p_value = spearmanr(pred, y)
    rho = 0.0 if np.isnan(rho) else float(rho)
    p_value = 1.0 if np.isnan(p_value) else float(p_value)
    deciles = decile_lift_table(pred, y)
    top_decile = deciles[-1]
    bottom_decile = deciles[0]
    log.info(
        "holdout: spearman rho=%.4f (p=%.4f), top-decile mean=%s (n=%s), bottom-decile mean=%s (n=%s)",
        rho, p_value, top_decile["mean_actual_payoff"], top_decile["n"],
        bottom_decile["mean_actual_payoff"], bottom_decile["n"],
    )
    return {
        "rows": int(len(y)),
        "spearman_rho": round(rho, 4),
        "spearman_p_value": round(p_value, 4),
        "decile_table": deciles,
        "top_decile_mean_payoff": top_decile["mean_actual_payoff"],
        "bottom_decile_mean_payoff": bottom_decile["mean_actual_payoff"],
    }


def compute_ship_gates(
    holdout_eval: Dict[str, Any],
    *,
    top_decile_mean_min: float = SHIP_GATE_TOP_DECILE_MEAN_MIN,
    spearman_min: float = SHIP_GATE_SPEARMAN_MIN,
    spearman_p_max: float = SHIP_GATE_SPEARMAN_P_MAX,
    min_holdout_rows: int = SHIP_GATE_MIN_HOLDOUT_ROWS,
) -> Dict[str, bool]:
    top_mean = holdout_eval.get("top_decile_mean_payoff")
    rho = holdout_eval.get("spearman_rho")
    p_value = holdout_eval.get("spearman_p_value")
    rows = holdout_eval.get("rows", 0)
    return {
        f"holdout_rows>={min_holdout_rows}": rows >= min_holdout_rows,
        f"top_decile_mean_payoff>{top_decile_mean_min}": top_mean is not None and top_mean > top_decile_mean_min,
        f"spearman_rho>={spearman_min}": rho is not None and rho >= spearman_min,
        f"spearman_p_value<={spearman_p_max}": p_value is not None and p_value <= spearman_p_max,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", required=True,
                    help="Output of build_option_pnl_training_view.py (features + payoff_score)")
    ap.add_argument("--output", required=True)
    ap.add_argument("--train-start", default="2024-11-04")
    ap.add_argument("--train-end", default="2026-04-30")
    ap.add_argument("--valid-start", default="2026-05-01")
    ap.add_argument("--valid-end", default="2026-05-31")
    ap.add_argument("--holdout-start", default="2026-06-01")
    ap.add_argument("--holdout-end", default="2026-07-31")
    ap.add_argument("--n-trials", type=int, default=60)
    ap.add_argument("--features", default="", help="Comma-separated feature override (empty=ENTRY_FEATURES_V3)")
    ap.add_argument("--winsorize-lower-pct", type=float, default=0.01)
    ap.add_argument("--winsorize-upper-pct", type=float, default=0.99)
    ap.add_argument("--no-winsorize", action="store_true", help="Train on the raw, unclipped target (not recommended)")
    args = ap.parse_args(argv)

    features = [f.strip() for f in args.features.split(",") if f.strip()] or list(ENTRY_FEATURES_V3)

    train_df = load_training_view(args.training_view_csv, start=args.train_start, end=args.train_end)
    valid_df = load_training_view(args.training_view_csv, start=args.valid_start, end=args.valid_end)
    holdout_df = load_training_view(args.training_view_csv, start=args.holdout_start, end=args.holdout_end)
    log.info(
        "rows: train=%d valid=%d holdout=%d (train %s..%s, valid %s..%s, holdout %s..%s)",
        len(train_df), len(valid_df), len(holdout_df),
        args.train_start, args.train_end, args.valid_start, args.valid_end, args.holdout_start, args.holdout_end,
    )
    if len(train_df) == 0 or len(valid_df) == 0 or len(holdout_df) == 0:
        raise ValueError("one of train/valid/holdout is empty -- check the date ranges against the CSV's actual coverage")

    X_train = _select_features(train_df, features)
    y_train = pd.to_numeric(train_df[TARGET_COL], errors="coerce").to_numpy()
    X_valid = _select_features(valid_df, features)
    y_valid = pd.to_numeric(valid_df[TARGET_COL], errors="coerce").to_numpy()
    X_hold = _select_features(holdout_df, features)
    y_hold = pd.to_numeric(holdout_df[TARGET_COL], errors="coerce").to_numpy()

    winsorize_bounds: Optional[tuple[float, float]] = None
    if not args.no_winsorize:
        raw_min, raw_max = float(np.min(y_train)), float(np.max(y_train))
        y_train, wlo, whi = winsorize_target(
            y_train, lower_pct=args.winsorize_lower_pct, upper_pct=args.winsorize_upper_pct,
        )
        n_clipped = int(np.sum((y_train == wlo) | (y_train == whi)) if wlo != whi else 0)
        log.info(
            "winsorized training target to [%.4f, %.4f] (raw range was [%.4f, %.4f], ~%d/%d rows clipped)",
            wlo, whi, raw_min, raw_max, n_clipped, len(y_train),
        )
        winsorize_bounds = (wlo, whi)

    # Feature medians from TRAIN only, for self-documented NaN-fill at inference
    # (same discipline as train_entry_dhan_v3 -- never NaN-pass-through to the model).
    medians = X_train.median(numeric_only=True).to_dict()
    X_train = X_train.fillna(medians)
    X_valid = X_valid.fillna(medians)
    X_hold = X_hold.fillna(medians)

    model, best_params, best_valid_rho = run_hpo(X_train, y_train, X_valid, y_valid, n_trials=args.n_trials)
    holdout_eval = evaluate_regression(model, X_hold, y_hold)
    gates = compute_ship_gates(holdout_eval)
    all_pass = all(gates.values())
    log.info("Ship gates: %s — %s", gates, "ALL_PASS" if all_pass else "FAIL")

    importances = {}
    try:
        for feat, imp in zip(features, model.feature_importances_):
            importances[feat] = round(float(imp), 6)
    except Exception:
        pass

    bundle = {
        "model": model,
        "features": features,
        "feature_medians": medians,
        "target": TARGET_COL,
        "label_definition": (
            "regression: max(net CE return, net PE return) holding the entry-bar's own ATM "
            "strike to a fixed horizon, net of realistic round-trip cost "
            "(ml_pipeline_2.pipeline.option_payoff_labels.compute_bar_payoff)"
        ),
        "best_params": best_params,
        "best_valid_spearman_rho": round(best_valid_rho, 4),
        "holdout_eval": holdout_eval,
        "ship_gates": gates,
        "ship_gates_all_pass": all_pass,
        "feature_importances": importances,
        "training_metadata": {
            "train_start": args.train_start, "train_end": args.train_end,
            "valid_start": args.valid_start, "valid_end": args.valid_end,
            "holdout_start": args.holdout_start, "holdout_end": args.holdout_end,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "training_view_csv": args.training_view_csv,
            "winsorize_bounds": winsorize_bounds,
        },
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, args.output)
    report_path = str(Path(args.output).with_suffix("")) + ".report.json"
    with open(report_path, "w") as f:
        json.dump(
            {k: v for k, v in bundle.items() if k != "model"}, f, indent=2, default=str,
        )
    log.info("wrote %s and %s", args.output, report_path)
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
