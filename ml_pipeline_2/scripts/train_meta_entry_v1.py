"""Data-prep (plus a minimal, optional training stub) for the NIFTY
entry-timing META-LABELING layer, v1 (2026-09-26).

Background: three independent prior attempts found that buying options the
moment a "primary" movement/entry model becomes confident loses money at
every horizon -- see ``ml_pipeline_2/src/ml_pipeline_2/pipeline/
meta_entry_features.py``'s module docstring for the full history and the
feature-design rationale. All three prior attempts traded EVERY primary
fire unconditionally. This script instead builds the training frame for a
SECONDARY ("meta") model trained ONLY on bars where the primary fired,
targeting whether that specific fire's cost-aware payoff
(``ml_pipeline_2.pipeline.option_payoff_labels.BarPayoff.payoff_score``)
actually cleared zero.

**This is a data-prep script.** The real primary model and the real
payoff-label pipeline are separate, parallel tracks -- this script accepts
their output as plain input columns (or synthetic stand-ins for now) and
does not import or depend on either implementation. `--fit` is a minimal,
non-HPO smoke-test of the training loop (a single XGBClassifier, no
calibration, no ship gates) -- swap in a real HPO study
(cf. ``train_entry_dhan_v3.run_hpo``) once the upstream tracks are real.

Input CSV contract (one row per bar, chronologically sorted):
    trade_date, timestamp                        -- optional, used for
                                                     passthrough + --fit's
                                                     train/holdout split
    <ml_pipeline_2.pipeline.meta_entry_features.SELECTED_VELOCITY_FEATURES>
                                                  -- already-computed
                                                     velocity/acceleration
                                                     columns (see
                                                     snapshot_app/core/
                                                     velocity_features.py)
    <--eligible-col, default "primary_eligible"> -- bool/0/1: did the
                                                     primary fire (or was
                                                     its probability above
                                                     threshold) on this bar
    <--probability-col, default "primary_probability">
                                                  -- primary's raw fire
                                                     probability (used only
                                                     to derive the coarse
                                                     bucket feature -- see
                                                     meta_entry_features.py
                                                     for why the raw value
                                                     itself is excluded)
    <--payoff-col, default "payoff_score">       -- from
                                                     option_payoff_labels.
                                                     BarPayoff.payoff_score
                                                     (or any stand-in with
                                                     the same "graded,
                                                     cost-aware,
                                                     direction-agnostic net
                                                     return" semantics);
                                                     blank/NaN = no usable
                                                     label, dropped (skip,
                                                     not impute)

Usage:
    # Data-prep only -- writes the filtered, feature-complete training frame.
    python -m ml_pipeline_2.scripts.train_meta_entry_v1 \\
        --input-csv .run/meta_entry/nifty_bars_with_primary_and_payoff.csv \\
        --output-csv .run/meta_entry/nifty_meta_training_frame.csv

    # Data-prep + a quick (non-HPO) XGBoost fit/report, split by trade_date.
    python -m ml_pipeline_2.scripts.train_meta_entry_v1 \\
        --input-csv .run/meta_entry/nifty_bars_with_primary_and_payoff.csv \\
        --output-csv .run/meta_entry/nifty_meta_training_frame.csv \\
        --fit --holdout-start 2026-08-01
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("train_meta_entry_v1")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.meta_entry_features import (  # noqa: E402
    BARS_SINCE_ELIGIBLE_COL,
    DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS,
    META_FEATURE_COLUMNS,
    bars_since_primary_first_eligible,
    build_meta_features_df,
)

TARGET_COL = "meta_target"

# Columns passed through to the output frame for traceability/splitting --
# never fed to the model itself (only META_FEATURE_COLUMNS + TARGET_COL are).
_PASSTHROUGH_COLS = ("trade_date", "timestamp")


def build_training_frame(
    df: pd.DataFrame,
    *,
    eligible_col: str = "primary_eligible",
    probability_col: str = "primary_probability",
    payoff_col: str = "payoff_score",
    prob_bucket_thresholds: Sequence[float] = DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS,
) -> pd.DataFrame:
    """Filter `df` to primary-fire bars with a usable payoff label, and
    assemble the meta-model training frame: META_FEATURE_COLUMNS +
    TARGET_COL (+ any of trade_date/timestamp present, for traceability).

    `df` must already be in chronological order -- the streak feature
    (`bars_since_primary_first_eligible`) is computed over the FULL input
    sequence before any filtering, since a fire's streak length depends on
    the bars before it, including ones that never fired.

    Never emits `eligible_col`, `probability_col` (raw) or `payoff_col`
    themselves -- only the coarse-bucketed/derived features survive into
    the output, by design (see meta_entry_features.py's module docstring).
    """
    required = {eligible_col, probability_col, payoff_col}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"build_training_frame: input frame missing required column(s): {sorted(missing)}")

    df = df.reset_index(drop=True).copy()

    # Streak computed over the FULL sequence -- must see every bar,
    # including ones that never fired, to know how long a streak has run.
    eligible_full = df[eligible_col].astype(bool).tolist()
    df[BARS_SINCE_ELIGIBLE_COL] = bars_since_primary_first_eligible(eligible_full)

    fired = df[df[eligible_col].astype(bool)].reset_index(drop=True)
    log.info("build_training_frame: %d/%d bars were primary-eligible fires", len(fired), len(df))
    if len(fired) == 0:
        log.warning("build_training_frame: zero fired bars -- nothing to train on")

    payoff_numeric = pd.to_numeric(fired[payoff_col], errors="coerce")
    has_payoff = payoff_numeric.notna()
    n_dropped = int((~has_payoff).sum())
    if n_dropped:
        log.info(
            "build_training_frame: dropping %d fired bar(s) with no usable payoff_score (skip, not impute)",
            n_dropped,
        )
    fired = fired[has_payoff].reset_index(drop=True)
    payoff_numeric = payoff_numeric[has_payoff].reset_index(drop=True)

    feats = build_meta_features_df(
        fired,
        primary_probability=fired[probability_col].tolist(),
        bars_since_first_eligible=fired[BARS_SINCE_ELIGIBLE_COL].tolist(),
        prob_bucket_thresholds=prob_bucket_thresholds,
    )
    feats[TARGET_COL] = (payoff_numeric > 0).astype(int).to_numpy()

    for col in _PASSTHROUGH_COLS:
        if col in fired.columns:
            feats.insert(0, col, fired[col].to_numpy())

    pos_rate = float(feats[TARGET_COL].mean()) if len(feats) else float("nan")
    log.info("build_training_frame: %d training rows, target positive_rate=%.3f", len(feats), pos_rate)
    return feats


def fit_meta_model(
    train_df: pd.DataFrame,
    holdout_df: pd.DataFrame,
    feature_cols: List[str] = META_FEATURE_COLUMNS,
) -> Dict[str, Any]:
    """Minimal (non-HPO) XGBoost fit + holdout AUC report. A stub -- the
    real deliverable of this script is `build_training_frame`; swap this
    for a real HPO study (see train_entry_dhan_v3.run_hpo for the house
    pattern) once the upstream primary-model + payoff-label tracks are
    wired to real data."""
    try:
        import xgboost as xgb
        from sklearn.metrics import roc_auc_score
    except ImportError as exc:
        raise ImportError(f"Install xgboost + scikit-learn to use --fit: {exc}")

    X_train = train_df[feature_cols].fillna(0.0)
    y_train = train_df[TARGET_COL].to_numpy()
    X_hold = holdout_df[feature_cols].fillna(0.0)
    y_hold = holdout_df[TARGET_COL].to_numpy()

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        eval_metric="auc",
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train, verbose=False)

    result: Dict[str, Any] = {"model": model, "n_train": int(len(y_train)), "n_holdout": int(len(y_hold))}
    if len(np.unique(y_hold)) < 2:
        log.warning("fit_meta_model: holdout has a single class -- AUC is undefined, skipping")
        result["holdout_auc"] = None
        return result

    prob = model.predict_proba(X_hold)[:, 1]
    auc = float(roc_auc_score(y_hold, prob))
    log.info("fit_meta_model: holdout AUC=%.4f (n_train=%d n_holdout=%d)", auc, len(y_train), len(y_hold))
    result["holdout_auc"] = auc
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input-csv", required=True,
                    help="Per-bar CSV: velocity features + eligibility + primary probability + payoff_score")
    ap.add_argument("--output-csv", required=True,
                    help="Where to write the filtered, feature-complete training frame")
    ap.add_argument("--eligible-col", default="primary_eligible")
    ap.add_argument("--probability-col", default="primary_probability")
    ap.add_argument("--payoff-col", default="payoff_score")
    ap.add_argument(
        "--prob-bucket-thresholds",
        default=",".join(str(t) for t in DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS),
        help="Comma-separated ascending thresholds for the coarse primary-probability bucket feature",
    )
    ap.add_argument("--fit", action="store_true",
                    help="Also fit a minimal (non-HPO) XGBoost model and report holdout AUC. "
                         "Data-prep (writing --output-csv) is this script's real deliverable.")
    ap.add_argument("--holdout-start", default=None,
                    help="trade_date (YYYY-MM-DD) at/after which rows go to the --fit holdout split; "
                         "requires a trade_date column in --input-csv")
    args = ap.parse_args(argv)

    thresholds = tuple(float(t) for t in args.prob_bucket_thresholds.split(",") if t.strip())

    in_path = Path(args.input_csv).resolve()
    log.info("Loading %s", in_path)
    df = pd.read_csv(in_path)

    training_frame = build_training_frame(
        df,
        eligible_col=args.eligible_col,
        probability_col=args.probability_col,
        payoff_col=args.payoff_col,
        prob_bucket_thresholds=thresholds,
    )

    out_path = Path(args.output_csv).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    training_frame.to_csv(out_path, index=False)
    log.info("Wrote training frame: %s (%d rows, %d cols)", out_path, len(training_frame), len(training_frame.columns))

    if args.fit:
        if not args.holdout_start:
            ap.error("--fit requires --holdout-start")
        if "trade_date" not in training_frame.columns:
            ap.error("--fit requires a trade_date column in --input-csv to split train/holdout")
        train_mask = training_frame["trade_date"].astype(str) < args.holdout_start
        train_df = training_frame[train_mask]
        holdout_df = training_frame[~train_mask]
        log.info("Fit split: train=%d holdout=%d (cut at %s)", len(train_df), len(holdout_df), args.holdout_start)
        fit_meta_model(train_df, holdout_df)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
