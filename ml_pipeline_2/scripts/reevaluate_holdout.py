"""Re-score an already-trained bundle's holdout split at a wider
threshold grid -- no retraining needed.

Born 2026-09-10: every one of a real 6-candidate overnight study's
auto-picked thresholds landed exactly at 0.70, the hard ceiling
train_entry_dhan_v3.evaluate() used to test up to. When every result in
a study lands at the edge of what was searched, that's a signal the
search itself was too narrow, not that the edge is genuinely optimal.
evaluate() now defaults to a grid up to 0.95 (DEFAULT_SEPARATION_THRESHOLDS)
for any NEW training run -- this script re-scores bundles that were
already trained under the old 0.70-ceiling grid, using the bundle's own
saved model/features/medians against the exact same holdout window,
so nothing needs to be retrained just to see the fuller table.

Usage (inside the strategy_app container, needs the same training_view
CSV the bundle was originally trained from):

    python3 -m ml_pipeline_2.scripts.reevaluate_holdout \\
        --bundle .run/overnight_study/sensex/SENSEX_010pct.joblib \\
        --training-view-csv .run/training_view_sensex/training_view_sensex.csv.gz
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
    ap.add_argument("--thresholds", default="",
                     help="comma-separated override grid (default: train_entry_dhan_v3.DEFAULT_SEPARATION_THRESHOLDS)")
    args = ap.parse_args(argv)

    sys.path.insert(0, "/app")
    import joblib
    from ml_pipeline_2.scripts.train_entry_dhan_v3 import (
        DEFAULT_SEPARATION_THRESHOLDS,
        add_labels,
        build_feature_matrix,
        evaluate,
        load_training_view_csv,
    )

    bundle = joblib.load(args.bundle)
    tm = bundle["training_metadata"]
    features = bundle["features"]
    medians = bundle["feature_medians"]
    model = bundle["model"]

    holdout_start, holdout_end = tm["holdout_window"].split("..")
    df = load_training_view_csv(Path(args.training_view_csv), tm["train_window"].split("..")[0], holdout_end)
    df = add_labels(df, label_pct=tm.get("label_pct"), label_pt=tm.get("label_pt"),
                     horizon_min=tm.get("label_horizon_min", 15))
    df = df[df["entry_label"].notna()].copy()

    holdout_df = df[(df["trade_date"] >= holdout_start) & (df["trade_date"] <= holdout_end)].copy()
    X_hold = build_feature_matrix(holdout_df, features, medians)
    y_hold = holdout_df["entry_label"].to_numpy(float)
    import numpy as np
    ok = np.isfinite(y_hold) & ~X_hold.isna().any(axis=1)
    X_hold, y_hold = X_hold[ok], y_hold[ok]

    thresholds = (
        [float(t) for t in args.thresholds.split(",") if t.strip()]
        if args.thresholds else DEFAULT_SEPARATION_THRESHOLDS
    )
    old_best = max(
        (r for r in bundle["holdout_eval"]["separation_table"] if not r.get("degenerate")),
        key=lambda r: r["separation"], default=None,
    )
    new_eval = evaluate(model, X_hold, y_hold, label="holdout_rescan", thresholds=thresholds)

    print(f"\n=== {args.bundle} ===")
    print(f"rows={new_eval['rows']} base_rate={new_eval['base_rate']} roc_auc={new_eval['roc_auc']}")
    print(f"Old grid's best-separation row: {old_best}")
    print("Full re-scanned separation table:")
    for row in new_eval["separation_table"]:
        print(f"  {row}")
    print(json.dumps(new_eval, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
