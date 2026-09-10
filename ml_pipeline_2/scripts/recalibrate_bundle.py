"""Re-fit an already-trained bundle's isotonic calibration on a WIDER
validation window, without re-running the (expensive, hours-long) Optuna
HPO -- the XGBoost hyperparameters don't need to change, only the
calibration step that maps its raw score to a probability.

Born 2026-09-10: a real 6-candidate study's validation window (2 weeks,
~3,200 rows -- an explicit config mistake, not a script-default problem;
train_entry_dhan_v3's own defaults use 2 months) produced under-resolved
isotonic calibration. Confirmed directly: two real August days with
genuinely different market conditions (VIX 11.71 vs 12.62, ADX 100 vs 28)
produced IDENTICAL calibrated probabilities (0.5188) from DIFFERENT raw
XGBoost scores (0.5496 vs 0.5511) -- the isotonic step covering that
whole raw-score region was fit from too few points to resolve it. This
silently inflated the model's "best" threshold to an unreachable extreme
(0.95, driven by a handful of very-high-raw-score holdout bars) while
backtests then correctly found zero trades, because 0.95 never recurred
-- even though the underlying model was fine and fired normally at
0.55-0.70 once recalibrated on a wider (~9,000+ row) window.

Usage (inside the strategy_app container):

    python3 -m ml_pipeline_2.scripts.recalibrate_bundle \\
        --bundle .run/overnight_study/nifty/NIFTY_010pct.joblib \\
        --training-view-csv .run/training_view/training_view_nifty.csv.gz \\
        --new-valid-weeks 6 \\
        --output .run/overnight_study/nifty/NIFTY_010pct.joblib   # in place
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--training-view-csv", required=True)
    ap.add_argument("--new-valid-weeks", type=int, default=6,
                     help="validation window size in weeks, ending at the bundle's original valid_end "
                          "(eats into what was --train-end, holdout is untouched)")
    ap.add_argument("--output", default=None, help="defaults to overwriting --bundle in place")
    args = ap.parse_args(argv)

    sys.path.insert(0, "/app")
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.calibration import CalibratedClassifierCV
    from ml_pipeline_2.scripts.train_entry_dhan_v3 import (
        MIN_VALID_ROWS_FOR_CALIBRATION,
        add_labels,
        build_feature_matrix,
        evaluate,
        load_training_view_csv,
    )

    bundle = joblib.load(args.bundle)
    tm = bundle["training_metadata"]
    features = bundle["features"]
    medians = bundle["feature_medians"]
    old_valid_start, old_valid_end = tm["valid_window"].split("..")
    holdout_start, holdout_end = tm["holdout_window"].split("..")

    try:
        base_est = bundle["model"].calibrated_classifiers_[0].estimator
    except AttributeError:
        base_est = bundle["model"].calibrated_classifiers_[0].base_estimator  # older sklearn

    df = load_training_view_csv(Path(args.training_view_csv), tm["train_window"].split("..")[0], holdout_end)
    df = add_labels(df, label_pct=tm.get("label_pct"), label_pt=tm.get("label_pt"),
                     horizon_min=tm.get("label_horizon_min", 15))
    df = df[df["entry_label"].notna()].copy()

    new_valid_start = (pd.Timestamp(old_valid_end) - pd.Timedelta(weeks=args.new_valid_weeks)).strftime("%Y-%m-%d")
    valid_df = df[(df["trade_date"] >= new_valid_start) & (df["trade_date"] <= old_valid_end)].copy()
    holdout_df = df[(df["trade_date"] >= holdout_start) & (df["trade_date"] <= holdout_end)].copy()

    X_valid = build_feature_matrix(valid_df, features, medians)
    y_valid = valid_df["entry_label"].to_numpy(float)
    ok = np.isfinite(y_valid) & ~X_valid.isna().any(axis=1)
    X_valid, y_valid = X_valid[ok], y_valid[ok]

    print(f"New validation window: {new_valid_start}..{old_valid_end} "
          f"({len(y_valid)} rows, vs original {tm['valid_window']} = {tm.get('n_valid', 'unknown')} rows)",
          file=sys.stderr)
    if len(y_valid) < MIN_VALID_ROWS_FOR_CALIBRATION:
        print(f"WARNING: still under MIN_VALID_ROWS_FOR_CALIBRATION={MIN_VALID_ROWS_FOR_CALIBRATION} -- "
              f"increase --new-valid-weeks or check data availability.", file=sys.stderr)

    try:
        cal = CalibratedClassifierCV(estimator=base_est, method="isotonic", cv="prefit")
    except TypeError:
        cal = CalibratedClassifierCV(base_estimator=base_est, method="isotonic", cv="prefit")
    cal.fit(X_valid, y_valid)

    X_hold = build_feature_matrix(holdout_df, features, medians)
    y_hold = holdout_df["entry_label"].to_numpy(float)
    ok = np.isfinite(y_hold) & ~X_hold.isna().any(axis=1)
    X_hold, y_hold = X_hold[ok], y_hold[ok]
    new_eval = evaluate(cal, X_hold, y_hold, label="holdout_recalibrated")

    bundle["model"] = cal
    bundle["holdout_eval"] = new_eval
    bundle["training_metadata"] = {
        **tm,
        "valid_window": f"{new_valid_start}..{old_valid_end}",
        "n_valid": int(len(y_valid)),
        "calibration_valid_rows_sufficient": len(y_valid) >= MIN_VALID_ROWS_FOR_CALIBRATION,
        "recalibrated_from": tm["valid_window"],
        "recalibrated_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }

    out_path = Path(args.output or args.bundle)
    joblib.dump(bundle, out_path)
    report_path = out_path.with_suffix(".report.json")
    report = {k: v for k, v in bundle.items() if k != "model"}
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"\nRecalibrated bundle written: {out_path}")
    print(f"Report: {report_path}")
    print(f"New holdout: AUC={new_eval['roc_auc']} rows={new_eval['rows']}")
    for row in new_eval["separation_table"]:
        print(f"  {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
