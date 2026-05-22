"""Quick baseline direction-only trainer (single XGB, no HPO).

For proper walk-forward CV + Optuna HPO + model catalog search, use instead:
    python -m ml_pipeline_2.scripts.run_direction_only_hpo
Manifest: configs/research/staged_dual_recipe.direction_only_hpo_v1.json

This script trains a simple binary CE-vs-PE classifier at the 11:30 bar without any
entry-quality gate — useful for a fast sanity check, not for production model selection.

Label construction
------------------
For each 11:30 row on date D, we look forward to a configurable horizon
(default: 150 minutes, i.e. ~14:00) and compute the futures price return.

    direction = CE (1)  if  fwd_return > +threshold  (market going up → buy call)
    direction = PE (0)  if  fwd_return < -threshold  (market going down → buy put)
    (excluded if |fwd_return| <= threshold — ambiguous)

This is strictly futures-based and does NOT need option P&L oracle labels,
making the script self-contained given only the v2/v3 flat parquet data.

Feature set
-----------
Uses all velocity (vel_*, ctx_am_*, ctx_gap_*), daily regime, and the
proven midday anchors (ema, ATR, vwap_distance, RSI, expiry context).
These are all available in snapshots_ml_flat_v2 at the 11:30 bar.

Output (--output-dir)
---------------------
  direction_only_model.joblib   — trained model bundle (XGBClassifier)
  direction_only_report.json    — performance report + metadata

Run on ML VM:
    /opt/option_trading/.venv/bin/python \
        ml_pipeline_2/scripts/train_direction_only.py \
        [--parquet-root /path/to/parquet_data] \
        [--start 2020-04-01] [--end 2024-12-31] \
        [--holdout-start 2024-01-01] \
        [--forward-minutes 150] [--threshold 0.003] \
        [--output-dir /path/to/output]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

# ── feature configuration (mirrors fo_velocity_v1 + fo_expiry_aware_v2) ──────
FEATURE_REGEXES = [
    # Velocity / morning context (v2 enrichment)
    r"^vel_",
    r"^ctx_am_",
    r"^adx_14$",
    r"^vol_spike_ratio$",
    r"^ctx_gap_",
    # Proven midday anchors
    r"^ema_",
    r"^vwap_distance$",
    r"^osc_rsi_14$",
    r"^osc_atr_",
    r"^near_atm_oi_ratio$",
    r"^atm_oi_ratio$",
    # Regime
    r"^ctx_regime_atr_high$",
    r"^ctx_regime_atr_low$",
    r"^ctx_regime_trend_up$",
    r"^ctx_regime_trend_down$",
    r"^ctx_is_high_vix_day$",
    r"^regime_rv20$",
    r"^regime_dist_sma20$",
    r"^regime_sma20_slope$",
    r"^regime_60d_return$",
    # Expiry / time
    r"^ctx_dte_days$",
    r"^ctx_is_expiry_day$",
    r"^ctx_is_near_expiry$",
    r"^time_minute_of_day$",
    r"^time_day_of_week$",
    # Options flow (intraday)
    r"^opt_flow_pcr_oi$",
    r"^pcr_change_5m$",
    r"^pcr_change_15m$",
    r"^opt_flow_ce_pe_oi_diff$",
    r"^fut_flow_oi_change_5m$",
    r"^dist_from_day_",
]

MIDDAY_MINUTE = 11 * 60 + 30   # 11:30 in minutes-since-midnight


def _resolve_parquet_root(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("OPTION_TRADING_PARQUET_ROOT", "").strip()
    if env:
        return Path(env)
    fallback = Path("/opt/option_trading/.data/parquet_data")
    if fallback.exists():
        return fallback
    raise SystemExit(
        "Cannot find parquet root. Set OPTION_TRADING_PARQUET_ROOT or pass --parquet-root."
    )


def _load_flat(flat_root: Path, start: str, end: str) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    years = range(start_ts.year, end_ts.year + 1)
    frames = []
    for y in years:
        for f in sorted(flat_root.glob(f"year={y}/*.parquet")):
            df = pd.read_parquet(f)
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            mask = (df["trade_date"] >= start_ts) & (df["trade_date"] <= end_ts)
            if mask.any():
                frames.append(df[mask])
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _select_features(df: pd.DataFrame) -> List[str]:
    import re
    available = set(df.columns)
    selected = []
    seen = set()
    for regex in FEATURE_REGEXES:
        pattern = re.compile(regex)
        for col in sorted(available):
            if col not in seen and pattern.match(col):
                selected.append(col)
                seen.add(col)
    return selected


def _build_labels(df: pd.DataFrame, forward_minutes: int, threshold: float) -> pd.DataFrame:
    """
    For each 11:30 row, compute forward futures return at 11:30 + forward_minutes.
    Returns a DataFrame with only 11:30 rows that have a valid forward label.
    """
    df = df.copy()
    df["_ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["_minute"] = df["_ts"].dt.hour * 60 + df["_ts"].dt.minute
    df["px_fut_close"] = pd.to_numeric(df["px_fut_close"], errors="coerce")

    # Target minute for forward price
    target_minute = MIDDAY_MINUTE + forward_minutes

    # Build a lookup: trade_date → minute → close price
    price_pivot = (
        df[["trade_date", "_minute", "px_fut_close"]]
        .dropna()
        .groupby(["trade_date", "_minute"])["px_fut_close"]
        .last()
        .reset_index()
        .rename(columns={"px_fut_close": "fwd_close"})
    )
    fwd_lookup = price_pivot[price_pivot["_minute"] == target_minute][
        ["trade_date", "fwd_close"]
    ]

    # Get 11:30 rows
    midday = df[df["_minute"] == MIDDAY_MINUTE].copy()

    # Join forward price
    midday = midday.merge(fwd_lookup, on="trade_date", how="inner")
    midday["fwd_return"] = (midday["fwd_close"] - midday["px_fut_close"]) / midday["px_fut_close"]

    # Label: CE=1 (up), PE=0 (down), drop ambiguous
    midday = midday.dropna(subset=["fwd_return"])
    ce_mask = midday["fwd_return"] > threshold
    pe_mask = midday["fwd_return"] < -threshold
    midday = midday[ce_mask | pe_mask].copy()
    midday["direction_label"] = np.where(midday["fwd_return"] > 0, "CE", "PE")
    midday["direction_y"] = (midday["direction_label"] == "CE").astype(int)

    return midday.reset_index(drop=True)


def _train_model(X_train: pd.DataFrame, y_train: pd.Series) -> object:
    try:
        from xgboost import XGBClassifier
        model = XGBClassifier(
            n_estimators=300,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.7,
            min_child_weight=10,
            scale_pos_weight=1.0,
            eval_metric="logloss",
            random_state=42,
            n_jobs=-1,
        )
    except ImportError:
        from sklearn.ensemble import GradientBoostingClassifier
        model = GradientBoostingClassifier(
            n_estimators=200,
            max_depth=3,
            learning_rate=0.05,
            subsample=0.8,
            random_state=42,
        )
    model.fit(X_train, y_train)
    return model


def _evaluate(model: object, X: pd.DataFrame, y: pd.Series, label: str) -> Dict:
    from sklearn.metrics import accuracy_score, roc_auc_score, classification_report
    probs = model.predict_proba(X)[:, 1]
    preds = (probs >= 0.5).astype(int)
    acc = float(accuracy_score(y, preds))
    try:
        auc = float(roc_auc_score(y, probs))
    except Exception:
        auc = float("nan")
    ce_count = int((y == 1).sum())
    pe_count = int((y == 0).sum())
    report = classification_report(y, preds, target_names=["PE", "CE"], output_dict=True)
    return {
        "label": label,
        "n": len(y),
        "ce_count": ce_count,
        "pe_count": pe_count,
        "accuracy": round(acc, 4),
        "roc_auc": round(auc, 4),
        "ce_precision": round(report["CE"]["precision"], 4),
        "ce_recall": round(report["CE"]["recall"], 4),
        "pe_precision": round(report["PE"]["precision"], 4),
        "pe_recall": round(report["PE"]["recall"], 4),
    }


def _feature_importance(model: object, features: List[str], top_n: int = 20) -> List[Dict]:
    try:
        importance = model.feature_importances_
        ranked = sorted(zip(features, importance), key=lambda x: -x[1])[:top_n]
        return [{"feature": f, "importance": round(float(imp), 6)} for f, imp in ranked]
    except Exception:
        return []


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parquet-root", default=None)
    p.add_argument("--dataset", default=None,
                   help="Snapshot dataset name, e.g. snapshots_ml_flat_v2 (auto-detected if omitted)")
    p.add_argument("--start", default="2020-04-01", help="Training window start date")
    p.add_argument("--end", default="2024-12-31", help="Training window end date (inclusive)")
    p.add_argument("--holdout-start", default="2024-01-01",
                   help="Holdout split — rows from this date onwards are test only")
    p.add_argument("--forward-minutes", type=int, default=150,
                   help="Minutes after 11:30 to sample the forward price (default 150 → ~14:00)")
    p.add_argument("--threshold", type=float, default=0.003,
                   help="Minimum |return| to count as CE/PE direction (default 0.003 = 0.3%%)")
    p.add_argument("--output-dir", default=None,
                   help="Where to write model bundle (default: ml_pipeline_2/artifacts/direction_only/)")
    p.add_argument("--dry-run", action="store_true",
                   help="Load data and print stats but do not train or save.")
    args = p.parse_args(argv)

    root = _resolve_parquet_root(args.parquet_root)

    if args.dataset:
        dataset_name = args.dataset
    else:
        for candidate in ("snapshots_ml_flat_v3", "snapshots_ml_flat_v2", "snapshots_ml_flat"):
            if (root / candidate).exists():
                dataset_name = candidate
                break
        else:
            raise SystemExit(f"No snapshots_ml_flat* dataset found under {root}")

    flat_root = root / dataset_name
    print(f"Dataset        : {flat_root}")
    print(f"Window         : {args.start} → {args.end}  (holdout from {args.holdout_start})")
    print(f"Forward horizon: {args.forward_minutes} min  threshold: ±{args.threshold*100:.2f}%")

    print("Loading data …")
    df = _load_flat(flat_root, args.start, args.end)
    if df.empty:
        raise SystemExit("No data loaded.")
    print(f"  Total rows: {len(df):,d}  dates: {df['trade_date'].nunique():,d}")

    print("Building direction labels …")
    labeled = _build_labels(df, args.forward_minutes, args.threshold)
    print(f"  Labeled 11:30 rows: {len(labeled):,d}")
    print(f"  CE: {(labeled['direction_label']=='CE').sum():,d}  "
          f"PE: {(labeled['direction_label']=='PE').sum():,d}  "
          f"Balance: {(labeled['direction_label']=='CE').mean():.1%} CE")

    features = _select_features(labeled)
    print(f"  Feature columns: {len(features)}")

    # Check velocity coverage
    vel_probe = "vel_price_delta_open"
    if vel_probe in labeled.columns:
        vel_cov = labeled[vel_probe].notna().mean()
        print(f"  Velocity coverage: {vel_cov:.1%}")
        if vel_cov < 0.5:
            print("  WARNING: Low velocity coverage — enrichment may be incomplete.")

    if args.dry_run:
        print("\n[dry-run] Exiting before training.")
        return

    # ── train/holdout split ───────────────────────────────────────────────────
    holdout_ts = pd.Timestamp(args.holdout_start)
    train_mask = labeled["trade_date"] < holdout_ts
    holdout_mask = labeled["trade_date"] >= holdout_ts

    train = labeled[train_mask].copy()
    holdout = labeled[holdout_mask].copy()
    print(f"\nTrain  : {len(train):,d} rows  ({train['trade_date'].nunique():,d} dates)")
    print(f"Holdout: {len(holdout):,d} rows  ({holdout['trade_date'].nunique():,d} dates)")

    if len(train) < 100:
        raise SystemExit("Not enough training rows. Check date range and velocity enrichment.")

    # Fill missing features with median (XGB handles NaN, but sklearn GBT does not)
    medians = train[features].median()
    X_train = train[features].fillna(medians)
    y_train = train["direction_y"]
    X_holdout = holdout[features].fillna(medians) if len(holdout) > 0 else pd.DataFrame()
    y_holdout = holdout["direction_y"] if len(holdout) > 0 else pd.Series(dtype=int)

    print("\nTraining direction classifier …")
    model = _train_model(X_train, y_train)

    # ── evaluation ────────────────────────────────────────────────────────────
    train_eval = _evaluate(model, X_train, y_train, "train")
    print(f"\nTrain  : acc={train_eval['accuracy']}  auc={train_eval['roc_auc']}  "
          f"CE_prec={train_eval['ce_precision']}  PE_prec={train_eval['pe_precision']}")

    holdout_eval: Optional[Dict] = None
    if len(holdout) > 10:
        holdout_eval = _evaluate(model, X_holdout, y_holdout, "holdout")
        print(f"Holdout: acc={holdout_eval['accuracy']}  auc={holdout_eval['roc_auc']}  "
              f"CE_prec={holdout_eval['ce_precision']}  PE_prec={holdout_eval['pe_precision']}")
    else:
        print("Holdout skipped (too few rows).")

    top_features = _feature_importance(model, features, top_n=20)
    print("\nTop features:")
    for item in top_features[:10]:
        print(f"  {item['feature']:45s}  {item['importance']:.4f}")

    # ── save bundle ───────────────────────────────────────────────────────────
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = _REPO_ROOT / "ml_pipeline_2" / "artifacts" / "direction_only"
    out_dir.mkdir(parents=True, exist_ok=True)

    import joblib
    bundle = {
        "kind": "direction_only_bundle",
        "model": model,
        "features": features,
        "feature_medians": medians.to_dict(),
        "label_map": {"CE": 1, "PE": 0},
        "training_config": {
            "dataset": dataset_name,
            "start": args.start,
            "end": args.end,
            "holdout_start": args.holdout_start,
            "forward_minutes": args.forward_minutes,
            "threshold": args.threshold,
        },
        "train_eval": train_eval,
        "holdout_eval": holdout_eval,
        "trained_at": datetime.utcnow().isoformat() + "Z",
    }

    model_path = out_dir / "direction_only_model.joblib"
    joblib.dump(bundle, model_path)
    print(f"\nModel bundle saved → {model_path}")

    report = {
        "kind": "direction_only_bundle",
        "trained_at": bundle["trained_at"],
        "training_config": bundle["training_config"],
        "n_features": len(features),
        "train_eval": train_eval,
        "holdout_eval": holdout_eval,
        "top_features": top_features,
    }
    report_path = out_dir / "direction_only_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"Report saved     → {report_path}")

    # Print guidance
    print("\nTo use in the deterministic engine:")
    print(f"  export DIRECTION_ML_MODEL_PATH={model_path}")
    print("  (then restart the strategy_app)")


if __name__ == "__main__":
    main()
