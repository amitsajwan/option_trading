"""Fast-onset entry model v1 — the crash detector.

Same question as the v3 entry model (|move| >= 100pt within 15 min), but with
FAST features (1-15 min windows) so it can react while a burst is underway.
Born 2026-07-08: NIFTY fell 336pts in 60min; the v3 model's 30-60m velocity
windows lagged the whole move (prob crossed 0.35 only at the bottom).

Deployed as ENTRY_ML_MODEL_PATH_2 (OR-combined with v3 in ml_entry.py): v3
catches slow builds, this catches fast expansions. Either can fire.

Design rules (train/serve parity is THE risk):
  - Bundle feature names are the SERVE names (what build_feature_row produces
    from a live snapshot). Training parquet columns are RENAMED to serve names.
  - Every alias was definition-audited 2026-07-08: ret_Nm == fut_return_Nm
    (same pct_change), osc_atr_14 == atr_14_1m (same TR + ewm alpha=1/14),
    vix == vix_current, opt_flow_atm_*_return_1m == atm_*_return_1m (training
    masks ATM-roll bars to NaN; serve may show a roll artifact - acceptable).
  - Label/splits/HPO/calibration REUSED from train_entry_dhan_v3 (leak-safe,
    already validated) - not reimplemented.

Usage (ML VM):
  .venv/bin/python -m ml_pipeline_2.scripts.train_entry_fast_v1 \
      --instrument NIFTY --data-dir .data/dhan_pipeline/indicators/nifty \
      --output models/nifty_entry_fast_v1.joblib \
      --train-start 2024-01-01 --train-end 2025-12-31 \
      --valid-start 2026-01-01 --valid-end 2026-04-30 \
      --holdout-start 2026-05-01 --holdout-end 2026-06-30 --n-trials 60
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml_pipeline_2.scripts.train_entry_dhan_v3 import (
    add_labels,
    load_indicators,
    run_hpo,
    evaluate,
    SESSION_START_MIN,
    SESSION_END_MIN,
    LABEL_PT_THRESHOLD,
    LABEL_HORIZON_MIN,
)

log = logging.getLogger("train_entry_fast_v1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Serve-name feature list (17). Parity proven 2026-07-08 against live Jul-8
# snapshots via build_feature_row (0% NaN post-warmup for tier A; tier B via
# definition-audited aliases below). vol_spike_ratio EXCLUDED: two colliding
# definitions exist across pipelines (0.025 vs 130.2 on the same bar).
FEATURES_FAST = [
    # tier A — identical names both sides
    "range_10", "range_30", "range_ratio_10_30",
    "bb_width_20", "bb_width_chg_5",
    "position_in_day_range", "vix_intraday_chg", "adx_14", "ema_spread_9_21",
    # tier B — parquet name renamed to serve name (definition-audited)
    "fut_return_1m", "fut_return_3m", "fut_return_5m", "fut_return_15m",
    "vix_current", "atr_14_1m",
    "atm_ce_return_1m", "atm_pe_return_1m",
]

RENAME_TRAIN_TO_SERVE = {
    "ret_1m": "fut_return_1m",
    "ret_3m": "fut_return_3m",
    "ret_5m": "fut_return_5m",
    "ret_15m": "fut_return_15m",
    "vix": "vix_current",
    "osc_atr_14": "atr_14_1m",
    "opt_flow_atm_call_return_1m": "atm_ce_return_1m",
    "opt_flow_atm_put_return_1m": "atm_pe_return_1m",
}

MAX_NAN_FEATURES = 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--train-start", required=True)
    ap.add_argument("--train-end", required=True)
    ap.add_argument("--valid-start", required=True)
    ap.add_argument("--valid-end", required=True)
    ap.add_argument("--holdout-start", required=True)
    ap.add_argument("--holdout-end", required=True)
    ap.add_argument("--n-trials", type=int, default=60)
    args = ap.parse_args()

    df = load_indicators(Path(args.data_dir).resolve(), args.instrument,
                         args.train_start, args.holdout_end)
    df = df.rename(columns=RENAME_TRAIN_TO_SERVE)
    missing = [f for f in FEATURES_FAST if f not in df.columns]
    if missing:
        raise SystemExit(f"FATAL: features missing from training data: {missing}")

    df = add_labels(df)
    df = df[df["entry_label"].notna()].copy()
    log.info("Labeled bars: %d, pos_rate=%.3f", len(df), df["entry_label"].mean())

    dts = pd.to_datetime(df["timestamp"])
    d = dts.dt.date.astype(str)
    train_df = df[(d >= args.train_start) & (d <= args.train_end)]
    valid_df = df[(d >= args.valid_start) & (d <= args.valid_end)]
    hold_df = df[(d >= args.holdout_start) & (d <= args.holdout_end)]
    log.info("Split train=%d valid=%d holdout=%d", len(train_df), len(valid_df), len(hold_df))

    medians = train_df[FEATURES_FAST].median(numeric_only=True).to_dict()

    def xy(part: pd.DataFrame):
        X = part[FEATURES_FAST].fillna(pd.Series(medians)).to_numpy(float)
        y = part["entry_label"].to_numpy(float)
        return X, y

    X_tr, y_tr = xy(train_df)
    X_va, y_va = xy(valid_df)
    X_ho, y_ho = xy(hold_df)

    model, best_params, hpo_auc = run_hpo(X_tr, y_tr, X_va, y_va, n_trials=args.n_trials)
    log.info("HPO best valid AUC=%.4f", hpo_auc)

    # Isotonic calibration on valid (prefit) — same recipe as v3.
    from sklearn.calibration import CalibratedClassifierCV
    calibrated = CalibratedClassifierCV(model, method="isotonic", cv="prefit")
    calibrated.fit(X_va, y_va)

    hold_eval = evaluate(calibrated, X_ho, y_ho, label="holdout")

    bundle = {
        "kind": "entry_only_bundle",
        "version": "fast_v1",
        "instrument": args.instrument,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "train_entry_fast_v1",
        "source_description": (
            f"{args.instrument} FAST entry model v1. Same label as v3 "
            f"(|move|>={LABEL_PT_THRESHOLD}pt in {LABEL_HORIZON_MIN}min) but 1-15min "
            "features for fast-onset detection. OR-combined with v3 via "
            "ENTRY_ML_MODEL_PATH_2."
        ),
        "label_definition": (
            f"binary: 1 if max(|high-close|,|close-low|) >= {LABEL_PT_THRESHOLD}pt "
            f"in next {LABEL_HORIZON_MIN}min"
        ),
        "features": FEATURES_FAST,
        "medians": medians,
        "max_nan_features": MAX_NAN_FEATURES,
        "model": calibrated,
        "holdout_eval": hold_eval,
        "training_metadata": {
            "instrument": args.instrument,
            "train_window": f"{args.train_start}..{args.train_end}",
            "valid_window": f"{args.valid_start}..{args.valid_end}",
            "holdout_window": f"{args.holdout_start}..{args.holdout_end}",
            "n_train": len(train_df),
            "n_holdout": len(hold_df),
            "pos_rate_train": float(train_df["entry_label"].mean()),
            "pos_rate_hold": float(hold_df["entry_label"].mean()),
            "hpo_trials": args.n_trials,
            "hpo_valid_auc": float(hpo_auc),
            "best_params": best_params,
            "session_window": f"{SESSION_START_MIN}-{SESSION_END_MIN} (min of day)",
            "rename_map": RENAME_TRAIN_TO_SERVE,
        },
    }

    out = Path(args.output)
    joblib.dump(bundle, out)
    report = {k: v for k, v in bundle.items() if k not in ("model", "medians")}
    Path(str(out).replace(".joblib", ".report.json")).write_text(
        json.dumps(report, indent=2, default=str))
    log.info("saved %s | holdout AUC=%.4f", out, hold_eval.get("roc_auc", float("nan")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
