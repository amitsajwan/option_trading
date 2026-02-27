import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    brier_score_loss,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from xgboost import XGBClassifier

from .config import TrainConfig
from .feature.profiles import (
    FEATURE_PROFILE_ALL,
    FEATURE_PROFILE_CORE_V1,
    FEATURE_PROFILE_CORE_V2,
    FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    FEATURE_PROFILES,
    apply_feature_profile,
)


IDENTITY_COLUMNS: Tuple[str, ...] = (
    "timestamp",
    "trade_date",
    "fut_symbol",
    "expiry_code",
    "source_day",
    "ce_symbol",
    "pe_symbol",
)

LABEL_COLUMNS: Tuple[str, ...] = (
    "label_horizon_minutes",
    "label_return_threshold",
    "ce_entry_price",
    "ce_exit_price",
    "ce_forward_return",
    "ce_mfe",
    "ce_mae",
    "ce_label_valid",
    "ce_label",
    "pe_entry_price",
    "pe_exit_price",
    "pe_forward_return",
    "pe_mfe",
    "pe_mae",
    "pe_label_valid",
    "pe_label",
    "pe_tp_hit",
    "pe_sl_hit",
    "pe_first_hit",
    "pe_first_hit_offset_min",
    "pe_path_exit_reason",
    "pe_tp_price",
    "pe_sl_price",
    "pe_time_stop_exit",
    "pe_hold_extension_eligible",
    "pe_trail_exit_price",
    "pe_trail_exit_offset_min",
    "pe_forced_eod_exit_price",
    "pe_forced_eod_exit_offset_min",
    "ce_tp_hit",
    "ce_sl_hit",
    "ce_first_hit",
    "ce_first_hit_offset_min",
    "ce_path_exit_reason",
    "ce_tp_price",
    "ce_sl_price",
    "ce_time_stop_exit",
    "ce_hold_extension_eligible",
    "ce_trail_exit_price",
    "ce_trail_exit_offset_min",
    "ce_forced_eod_exit_price",
    "ce_forced_eod_exit_offset_min",
    "ce_path_target_valid",
    "pe_path_target_valid",
    "best_side_label",
)

def _ensure_time_sorted(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return out


def select_feature_columns(df: pd.DataFrame, feature_profile: str = FEATURE_PROFILE_ALL) -> List[str]:
    excluded = set(IDENTITY_COLUMNS) | set(LABEL_COLUMNS)
    numeric_cols = list(df.select_dtypes(include=[np.number]).columns)
    selected = [col for col in numeric_cols if col not in excluded]
    return apply_feature_profile(selected, feature_profile=feature_profile)


def chronological_split(
    df: pd.DataFrame,
    train_ratio: float,
    valid_ratio: float,
) -> Dict[str, pd.DataFrame]:
    if not (0.0 < train_ratio < 1.0):
        raise ValueError("train_ratio must be in (0,1)")
    if not (0.0 < valid_ratio < 1.0):
        raise ValueError("valid_ratio must be in (0,1)")
    if train_ratio + valid_ratio >= 1.0:
        raise ValueError("train_ratio + valid_ratio must be < 1")

    ordered = _ensure_time_sorted(df)
    n = len(ordered)
    if n < 12:
        raise ValueError("not enough rows for chronological split (need >= 12)")

    train_end = int(np.floor(n * train_ratio))
    valid_end = int(np.floor(n * (train_ratio + valid_ratio)))

    train_end = max(train_end, 6)
    valid_end = max(valid_end, train_end + 3)
    valid_end = min(valid_end, n - 1)

    train = ordered.iloc[:train_end].copy()
    valid = ordered.iloc[train_end:valid_end].copy()
    test = ordered.iloc[valid_end:].copy()
    if len(valid) == 0 or len(test) == 0:
        raise ValueError("split produced empty valid/test partition")
    return {"train": train, "valid": valid, "test": test}


def compute_metrics(y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5) -> Dict[str, Optional[float]]:
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)
    y_pred = (y_prob >= threshold).astype(int)

    metrics: Dict[str, Optional[float]] = {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "brier": float(brier_score_loss(y_true, y_prob)),
    }
    classes = np.unique(y_true)
    has_both = len(classes) >= 2
    metrics["roc_auc"] = float(roc_auc_score(y_true, y_prob)) if has_both else None
    metrics["pr_auc"] = float(average_precision_score(y_true, y_prob)) if has_both else None
    metrics["positive_rate"] = float(np.mean(y_true))
    metrics["prediction_rate"] = float(np.mean(y_pred))
    return metrics


def build_baseline_pipeline(config: TrainConfig) -> Pipeline:
    model = XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        n_estimators=config.n_estimators,
        max_depth=config.max_depth,
        learning_rate=config.learning_rate,
        random_state=config.random_state,
        seed=config.random_state,
        n_jobs=1,
        subsample=1.0,
        colsample_bytree=1.0,
        tree_method="hist",
        verbosity=0,
    )
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("model", model),
        ]
    )


def _split_report(split_df: pd.DataFrame, target_col: str) -> Dict[str, object]:
    y = split_df[target_col].astype(int)
    return {
        "rows": int(len(split_df)),
        "positive_rows": int(y.sum()),
        "positive_rate": float(y.mean()) if len(y) else 0.0,
        "start": str(split_df["timestamp"].iloc[0]) if len(split_df) else None,
        "end": str(split_df["timestamp"].iloc[-1]) if len(split_df) else None,
    }


def _feature_importance(model: Pipeline, feature_columns: Sequence[str], top_k: int = 30) -> List[Dict[str, float]]:
    inner = model.named_steps["model"]
    values = getattr(inner, "feature_importances_", None)
    if values is None:
        return []
    pairs = []
    for name, score in zip(feature_columns, values):
        pairs.append({"feature": str(name), "importance": float(score)})
    pairs.sort(key=lambda x: x["importance"], reverse=True)
    return pairs[:top_k]


def _train_single_side(
    df: pd.DataFrame,
    side: str,
    feature_columns: Sequence[str],
    config: TrainConfig,
) -> Tuple[Pipeline, Dict[str, object]]:
    target_col = f"{side}_label"
    valid_col = f"{side}_label_valid"
    work = df[(df[valid_col] == 1.0) & df[target_col].notna()].copy()
    work[target_col] = work[target_col].astype(int)
    splits = chronological_split(work, train_ratio=config.train_ratio, valid_ratio=config.valid_ratio)

    train = splits["train"]
    valid = splits["valid"]
    test = splits["test"]

    x_train = train.loc[:, list(feature_columns)]
    y_train = train[target_col].to_numpy()
    x_valid = valid.loc[:, list(feature_columns)]
    y_valid = valid[target_col].to_numpy()
    x_test = test.loc[:, list(feature_columns)]
    y_test = test[target_col].to_numpy()

    pipe = build_baseline_pipeline(config)
    pipe.fit(x_train, y_train)

    valid_prob = pipe.predict_proba(x_valid)[:, 1]
    test_prob = pipe.predict_proba(x_test)[:, 1]

    report = {
        "rows_total": int(len(work)),
        "splits": {
            "train": _split_report(train, target_col),
            "valid": _split_report(valid, target_col),
            "test": _split_report(test, target_col),
        },
        "metrics": {
            "valid": compute_metrics(y_valid, valid_prob),
            "test": compute_metrics(y_test, test_prob),
        },
        "feature_importance": _feature_importance(pipe, feature_columns),
    }
    return pipe, report


def train_baseline_models(
    labeled_df: pd.DataFrame,
    config: TrainConfig,
    feature_profile: str = FEATURE_PROFILE_ALL,
) -> Tuple[Dict[str, object], Dict[str, Pipeline]]:
    frame = _ensure_time_sorted(labeled_df)
    feature_columns = select_feature_columns(frame, feature_profile=feature_profile)
    if not feature_columns:
        raise ValueError("no feature columns selected for training")

    ce_model, ce_report = _train_single_side(frame, side="ce", feature_columns=feature_columns, config=config)
    pe_model, pe_report = _train_single_side(frame, side="pe", feature_columns=feature_columns, config=config)

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": int(len(frame)),
        "feature_profile": str(feature_profile),
        "feature_count": int(len(feature_columns)),
        "feature_columns": feature_columns,
        "train_config": asdict(config),
        "ce": ce_report,
        "pe": pe_report,
    }
    models = {"ce": ce_model, "pe": pe_model}
    return report, models


def save_training_artifacts(
    report: Dict[str, object],
    models: Dict[str, Pipeline],
    model_out: Path,
    report_out: Path,
) -> None:
    model_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.parent.mkdir(parents=True, exist_ok=True)

    package = {
        "kind": "t06_baseline_model_package",
        "created_at_utc": report["created_at_utc"],
        "feature_profile": report.get("feature_profile", FEATURE_PROFILE_ALL),
        "feature_columns": report["feature_columns"],
        "train_config": report["train_config"],
        "models": models,
    }
    joblib.dump(package, model_out)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Train baseline CE/PE models")
    parser.add_argument(
        "--labeled-data",
        default="ml_pipeline/artifacts/t05_labeled_features.parquet",
        help="Labeled feature parquet input",
    )
    parser.add_argument(
        "--model-out",
        default="ml_pipeline/artifacts/t06_baseline_model.joblib",
        help="Output model package",
    )
    parser.add_argument(
        "--report-out",
        default="ml_pipeline/artifacts/t06_train_report.json",
        help="Output training report JSON",
    )
    parser.add_argument("--train-ratio", type=float, default=None, help="Train split ratio")
    parser.add_argument("--valid-ratio", type=float, default=None, help="Validation split ratio")
    parser.add_argument("--random-state", type=int, default=None, help="Random seed")
    parser.add_argument("--max-depth", type=int, default=None, help="XGBoost max depth")
    parser.add_argument("--n-estimators", type=int, default=None, help="XGBoost number of trees")
    parser.add_argument("--learning-rate", type=float, default=None, help="XGBoost learning rate")
    parser.add_argument(
        "--feature-profile",
        default=FEATURE_PROFILE_ALL,
        choices=list(FEATURE_PROFILES),
        help="Feature set profile to train",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    if not labeled_path.exists():
        print(f"ERROR: labeled dataset not found: {labeled_path}")
        return 2

    default_cfg = TrainConfig()
    cfg = TrainConfig(
        train_ratio=args.train_ratio if args.train_ratio is not None else default_cfg.train_ratio,
        valid_ratio=args.valid_ratio if args.valid_ratio is not None else default_cfg.valid_ratio,
        random_state=args.random_state if args.random_state is not None else default_cfg.random_state,
        max_depth=args.max_depth if args.max_depth is not None else default_cfg.max_depth,
        n_estimators=args.n_estimators if args.n_estimators is not None else default_cfg.n_estimators,
        learning_rate=args.learning_rate if args.learning_rate is not None else default_cfg.learning_rate,
    )

    df = pd.read_parquet(labeled_path)
    report, models = train_baseline_models(df, config=cfg, feature_profile=str(args.feature_profile))
    model_out = Path(args.model_out)
    report_out = Path(args.report_out)
    save_training_artifacts(report, models, model_out=model_out, report_out=report_out)

    print(f"Input rows: {len(df)}")
    print(f"Feature profile: {report['feature_profile']}")
    print(f"Features used: {report['feature_count']}")
    print(f"CE valid ROC-AUC: {report['ce']['metrics']['valid']['roc_auc']}")
    print(f"PE valid ROC-AUC: {report['pe']['metrics']['valid']['roc_auc']}")
    print(f"Model: {model_out}")
    print(f"Report: {report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())

