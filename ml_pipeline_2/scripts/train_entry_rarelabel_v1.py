"""Train entry_only_bundle with the Kite-recipe RARE, level-invariant label.

Pre-registered 2026-07-12 (docs/ENTRY_MODEL_RARELABEL_REBUILD_2026-07-12.md).
Reuses train_entry_dhan_v3.py's HPO/calibration/evaluation machinery
verbatim; the only real changes are (1) label = 0.20% move in 5min, either
direction, level-invariant (base rate ~6%, matches the Kite-era model that
held AUC 0.827) instead of the live bundle's 100pt/15min (base rate ~33%,
AUC 0.676); (2) data source = the Mongo-based, quality-gated, EMA/max_pain-
enriched training view (build_training_view_from_mongo.py output), which
goes through the SAME build_feature_row serving path as live inference —
not the older parquet pipeline.

Usage:
    python -m ml_pipeline_2.scripts.train_entry_rarelabel_v1 \\
        --instrument BANKNIFTY \\
        --training-view /shared_run/training_view/training_view_banknifty.csv.gz \\
        --output /shared_run/training_view/entry_rarelabel_banknifty_v1.joblib \\
        --train-start 2024-11-01 --train-end 2026-03-31 \\
        --valid-start 2026-04-01 --valid-end 2026-05-31 \\
        --holdout-start 2026-06-01 --holdout-end 2026-07-10
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("train_entry_rarelabel_v1")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from contracts_app.instruments import known_instruments  # noqa: E402
from ml_pipeline_2.scripts.train_entry_dhan_v3 import (  # noqa: E402
    ENTRY_FEATURES_V3, build_feature_matrix, compute_medians, evaluate, run_hpo,
)

LABEL_HORIZON_MIN = 5
SESSION_START_MIN = 9 * 60 + 45
SESSION_END_MIN = 15 * 60 + 5


def add_rare_label(df: pd.DataFrame, label_pct: float) -> pd.DataFrame:
    """1 if fwd_maxabs_5m / fut_close >= label_pct. Reuses the training view's
    own no-lookahead-beyond-day forward window (fwd_maxabs_5m already NaN
    at day-end where a full 5m window isn't available)."""
    df = df.copy()
    pct_move = df["fwd_maxabs_5m"] / df["fut_close"].replace(0, np.nan)
    df["entry_label"] = (pct_move >= label_pct).astype(float)
    df.loc[df["fwd_maxabs_5m"].isna() | df["fut_close"].isna(), "entry_label"] = np.nan
    if "time" in df.columns:
        t = pd.to_datetime(df["time"], format="%H:%M:%S", errors="coerce")
        minute_of_day = t.dt.hour * 60 + t.dt.minute
        outside = (minute_of_day < SESSION_START_MIN) | (minute_of_day > SESSION_END_MIN)
        df.loc[outside.fillna(False), "entry_label"] = np.nan
    valid = df["entry_label"].notna()
    log.info("Labels: %d valid bars, pos_rate=%.4f", int(valid.sum()),
              float(df.loc[valid, "entry_label"].mean()))
    return df


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", required=True, choices=known_instruments())
    ap.add_argument("--training-view", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--train-start", default="2024-11-01")
    ap.add_argument("--train-end", default="2026-03-31")
    ap.add_argument("--valid-start", default="2026-04-01")
    ap.add_argument("--valid-end", default="2026-05-31")
    ap.add_argument("--holdout-start", default="2026-06-01")
    ap.add_argument("--holdout-end", default="2026-07-10")
    ap.add_argument("--n-trials", type=int, default=60)
    ap.add_argument("--label-pct", type=float, default=0.002,
                     help="Level-invariant move threshold, e.g. 0.002=0.20%% (Kite recipe)")
    args = ap.parse_args(argv)

    features = ENTRY_FEATURES_V3
    log.info("Instrument=%s | Features=%d (ENTRY_FEATURES_V3, live-bundle-compatible)",
              args.instrument, len(features))

    log.info("Loading training view: %s", args.training_view)
    df = pd.read_csv(args.training_view, compression="infer")
    log.info("Loaded %d rows, columns=%d", len(df), len(df.columns))
    missing = [f for f in features if f not in df.columns]
    if missing:
        log.warning("%d features missing from training view (will be all-NaN): %s",
                    len(missing), missing)

    df = add_rare_label(df, args.label_pct)
    df = df[df["entry_label"].notna()].copy()
    log.info("After label filter: %d bars, pos_rate=%.4f", len(df), df["entry_label"].mean())

    def _slice(start: str, end: str) -> pd.DataFrame:
        m = (df["trade_date"] >= start) & (df["trade_date"] <= end)
        return df[m].copy()

    train_df = _slice(args.train_start, args.train_end)
    valid_df = _slice(args.valid_start, args.valid_end)
    holdout_df = _slice(args.holdout_start, args.holdout_end)
    log.info("Split — train=%d valid=%d holdout=%d", len(train_df), len(valid_df), len(holdout_df))

    medians = compute_medians(train_df, features)
    X_train = build_feature_matrix(train_df, features, medians)
    y_train = train_df["entry_label"].to_numpy(float)
    X_valid = build_feature_matrix(valid_df, features, medians)
    y_valid = valid_df["entry_label"].to_numpy(float)
    X_hold = build_feature_matrix(holdout_df, features, medians)
    y_hold = holdout_df["entry_label"].to_numpy(float)

    ok_tr = np.isfinite(y_train) & ~X_train.isna().any(axis=1)
    ok_va = np.isfinite(y_valid) & ~X_valid.isna().any(axis=1)
    ok_ho = np.isfinite(y_hold) & ~X_hold.isna().any(axis=1)
    X_train, y_train = X_train[ok_tr], y_train[ok_tr]
    X_valid, y_valid = X_valid[ok_va], y_valid[ok_va]
    X_hold, y_hold = X_hold[ok_ho], y_hold[ok_ho]
    log.info("Clean rows — train=%d (pos=%.4f) valid=%d (pos=%.4f) holdout=%d (pos=%.4f)",
              len(y_train), y_train.mean(), len(y_valid), y_valid.mean(),
              len(y_hold), y_hold.mean())

    log.info("Starting Optuna HPO (%d trials)...", args.n_trials)
    model, best_params, valid_auc = run_hpo(X_train, y_train, X_valid, y_valid, args.n_trials)

    log.info("Fitting isotonic calibration on validation set...")
    from sklearn.calibration import CalibratedClassifierCV
    try:
        cal = CalibratedClassifierCV(estimator=model, method="isotonic", cv="prefit")
    except TypeError:
        cal = CalibratedClassifierCV(base_estimator=model, method="isotonic", cv="prefit")
    cal.fit(X_valid, y_valid)

    holdout_eval = evaluate(cal, X_hold, y_hold, "holdout")

    auc = holdout_eval["roc_auc"]
    gates = {
        "auc>=0.70": auc >= 0.70,
        "separation>=0.15": any((r.get("separation") or 0) >= 0.15
                                for r in holdout_eval["separation_table"]
                                if not r.get("degenerate")),
        "ece<=0.05": holdout_eval["ece"] <= 0.05,
        "prob_spread>=0.20": True,
    }
    p = cal.predict_proba(X_hold)[:, 1]
    gates["prob_spread>=0.20"] = bool(float(p.max()) - float(p.min()) >= 0.20)
    all_pass = all(gates.values())
    log.info("Ship gates: %s — %s", gates, "ALL_PASS" if all_pass else "FAIL")

    importances = {}
    try:
        raw_imp = model.feature_importances_
        for feat, imp in zip(features, raw_imp):
            importances[feat] = round(float(imp), 6)
        log.info("Top-5 features: %s", sorted(importances.items(), key=lambda x: -x[1])[:5])
    except Exception:
        pass

    bundle: Dict[str, Any] = {
        "kind": "entry_only_bundle",
        "version": "rarelabel_v1",
        "instrument": args.instrument,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "train_entry_rarelabel_v1",
        "source_description": (
            f"{args.instrument} entry model, RARE label recipe (2026-07-12 rebuild, "
            f"pre-registered docs/ENTRY_MODEL_RARELABEL_REBUILD_2026-07-12.md). "
            f"Binary label: |move|>={args.label_pct*100:.2f}% (level-invariant) in "
            f"{LABEL_HORIZON_MIN}min, either direction — matches the Kite-era "
            f"entry_only_v3 recipe (AUC 0.827) that the live bundle's common/broad "
            f"label (100pt/15min, AUC 0.676) regressed from."
        ),
        "features": features,
        "medians": medians,
        "feature_medians": medians,
        "max_nan_features": 3,
        "model": cal,
        "holdout_eval": holdout_eval,
        "feature_importances": importances,
        "ship_gates": gates,
        "ship_gates_all_pass": all_pass,
        "label_definition": (
            f"binary: 1 if max(|fut_close[t+k]-fut_close[t]|)/fut_close[t] >= "
            f"{args.label_pct} for k in 1..{LABEL_HORIZON_MIN}min"
        ),
        "training_metadata": {
            "instrument": args.instrument,
            "train_window": f"{args.train_start}..{args.train_end}",
            "valid_window": f"{args.valid_start}..{args.valid_end}",
            "holdout_window": f"{args.holdout_start}..{args.holdout_end}",
            "n_train": int(len(y_train)),
            "n_holdout": int(len(y_hold)),
            "pos_rate_train": round(float(y_train.mean()), 4),
            "pos_rate_hold": round(float(y_hold.mean()), 4),
            "hpo_trials": args.n_trials,
            "hpo_valid_auc": round(float(valid_auc), 4),
            "best_params": best_params,
            "label_pct": args.label_pct,
            "label_horizon_min": LABEL_HORIZON_MIN,
            "calibration": "isotonic_prefit_on_valid",
            "kite_reference_auc": 0.8274,
            "live_bundle_reference_auc": 0.6762,
        },
    }

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out)
    report = {k: v for k, v in bundle.items() if k != "model"}
    out.with_suffix(".report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")

    log.info("Bundle written: %s (AUC=%.4f vs Kite 0.827 vs live 0.676, ALL_PASS=%s)",
              out, auc, all_pass)
    return 0 if all_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
