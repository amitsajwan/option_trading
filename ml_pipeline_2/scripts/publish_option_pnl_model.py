"""Train ATM_PE_15 (or similar) with HPO config and save runtime-ready bundle.

Output bundle layout:
    {out_root}/{run_id}/
        model.joblib          — fitted XGBClassifier
        feature_columns.json  — ordered list of column names the model expects
        metadata.json         — recipe params, threshold, training info, contract version

Bundle is consumed at runtime by strategy_app/engines/option_pnl_predictor.py
which builds a StagedRuntimeDecision shape so the existing PureMLEngine
flow can use it without code changes elsewhere.

Training set: train + valid combined (max data for production). Holdout
is NOT held out here — we already validated edge via walk-forward; this
bundle is what we deploy. For research iteration use train_option_pnl_mvp.py
which preserves holdout for honest evaluation.

Usage:
    python -m ml_pipeline_2.scripts.publish_option_pnl_model \\
      --recipe ATM_PE_15 \\
      --labels-root /opt/option_trading/.data/ml_pipeline/parquet_data/option_pnl_labels_v1 \\
      --hpo-results-json /opt/option_trading/.data/ml_pipeline/option_pnl_hpo_PE15_20260517_1203/hpo_results.json \\
      --threshold 0.55 \\
      --recipe-params '{"option_type":"PE","strike_offset_steps":0,"max_hold_bars":15,"stop_pct_of_premium":0.25,"target_pct_of_premium":0.40}' \\
      --out /opt/option_trading/.data/ml_pipeline/option_pnl_published_models
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

try:
    import xgboost as xgb
except ImportError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(2)

from ml_pipeline_2.scripts.train_option_pnl_mvp import (
    DEFAULT_FLAT_ROOT, HOLDOUT_END, TRAIN_END, VALID_END,
    load_labels_and_features, select_feature_columns,
)


def run(args) -> int:
    labels_root = Path(args.labels_root)
    flat_root = Path(args.flat_root)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    # Resolve HPO params (best trial) or use defaults
    if args.hpo_results_json:
        hpo = json.loads(Path(args.hpo_results_json).read_text())
        if "trials" in hpo:
            params = hpo["trials"][0]["params"]
            print(f"using HPO best-trial params: {json.dumps(params, sort_keys=True)}")
        else:
            params = hpo
    else:
        params = dict(n_estimators=300, max_depth=4, learning_rate=0.05,
                      subsample=0.85, colsample_bytree=0.85, reg_lambda=2.0)
        print(f"using default params: {json.dumps(params, sort_keys=True)}")

    recipe_params = json.loads(args.recipe_params)
    threshold = float(args.threshold)

    print(f"\n=== Publish option-P&L model bundle ===")
    print(f"recipe: {args.recipe}")
    print(f"threshold: {threshold}")
    print(f"recipe_params: {recipe_params}")
    print(f"labels: {labels_root}")
    print(f"out: {out_root}")

    # Load data
    df = load_labels_and_features(labels_root, flat_root, args.recipe)
    feat_cols = select_feature_columns(df)
    if not feat_cols:
        print("ERROR: no feature columns selected", file=sys.stderr)
        return 1
    print(f"\nfeatures: {len(feat_cols)}")
    print(f"total rows: {len(df)}")

    # Train on train + valid (production: maximize data, holdout reserved for honest eval elsewhere)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    train = df[df["trade_date"] <= VALID_END]
    if len(train) < 1000:
        print(f"ERROR: too few train rows: {len(train)}", file=sys.stderr)
        return 1
    print(f"train rows: {len(train)} ({train['trade_date'].min().date()} → {train['trade_date'].max().date()})")
    print(f"label positive rate: {train['label'].mean():.4f}")

    X = train[feat_cols].fillna(0.0).to_numpy(dtype=np.float32)
    y = train["label"].astype(int).to_numpy()

    model = xgb.XGBClassifier(
        **params,
        objective="binary:logistic", eval_metric="auc",
        tree_method="hist", n_jobs=4, random_state=42,
    )
    print("\nfitting...")
    model.fit(X, y, verbose=False)

    # Create bundle directory
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    run_id = f"option_pnl_{args.recipe.lower()}_{timestamp}"
    bundle_dir = out_root / run_id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    model_path = bundle_dir / "model.joblib"
    joblib.dump(model, model_path)
    print(f"\nwrote model: {model_path}  ({model_path.stat().st_size // 1024} KB)")

    (bundle_dir / "feature_columns.json").write_text(
        json.dumps({"feature_columns": feat_cols, "n_features": len(feat_cols)}, indent=2)
    )

    metadata = {
        "bundle_version": "option_pnl_v1",
        "run_id": run_id,
        "recipe_id": args.recipe,
        "recipe_params": recipe_params,
        "decision_threshold": threshold,
        "model_family": "xgboost",
        "model_params": params,
        "training": {
            "labels_root": str(labels_root),
            "n_train_rows": int(len(train)),
            "train_date_min": str(train["trade_date"].min().date()),
            "train_date_max": str(train["trade_date"].max().date()),
            "label_positive_rate": float(train["label"].mean()),
            "n_features": len(feat_cols),
        },
        "runtime_contract_pointer": "ml_pipeline_2/configs/research/option_label_contract.json",
        "published_at_utc": datetime.utcnow().isoformat(),
        "publish_source": "ml_pipeline_2/scripts/publish_option_pnl_model.py",
    }
    (bundle_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    print(f"wrote feature_columns.json + metadata.json")

    print(f"\n=== Bundle ready ===")
    print(f"bundle dir: {bundle_dir}")
    print(f"\nFor runtime, set env:")
    print(f"  OPTION_PNL_MODEL_BUNDLE={bundle_dir}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Train + publish option-P&L model bundle for runtime")
    p.add_argument("--recipe", required=True, help="e.g. ATM_PE_15")
    p.add_argument("--labels-root", required=True)
    p.add_argument("--flat-root", default=str(DEFAULT_FLAT_ROOT))
    p.add_argument("--hpo-results-json", default=None,
                   help="If set, uses best-trial params from this HPO results file. Otherwise default XGBoost params.")
    p.add_argument("--threshold", type=float, required=True,
                   help="Probability threshold at which to fire trade.")
    p.add_argument("--recipe-params", required=True,
                   help='JSON: {"option_type":"PE","strike_offset_steps":0,"max_hold_bars":15,"stop_pct_of_premium":0.25,"target_pct_of_premium":0.40}')
    p.add_argument("--out", required=True)
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
