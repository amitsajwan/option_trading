import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from .config import TrainConfig
from .train_baseline import FEATURE_PROFILE_ALL, FEATURE_PROFILES, build_baseline_pipeline, compute_metrics, select_feature_columns


def _to_sorted_days(df: pd.DataFrame) -> List[str]:
    return sorted(df["trade_date"].astype(str).unique().tolist())


def build_day_folds(
    days: Sequence[str],
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    purge_days: int = 0,
    embargo_days: int = 0,
) -> List[Dict[str, List[str]]]:
    if train_days <= 0 or valid_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError("train_days, valid_days, test_days, step_days must all be > 0")
    if purge_days < 0 or embargo_days < 0:
        raise ValueError("purge_days and embargo_days must be >= 0")
    n = len(days)
    span = train_days + purge_days + valid_days + embargo_days + test_days
    folds: List[Dict[str, List[str]]] = []
    start = 0
    while start + span <= n:
        train_start = start
        train_end = train_start + train_days
        valid_start = train_end + purge_days
        valid_end = valid_start + valid_days
        test_start = valid_end + embargo_days
        test_end = test_start + test_days

        train_slice = list(days[train_start:train_end])
        purge_slice = list(days[train_end:valid_start])
        valid_slice = list(days[valid_start:valid_end])
        embargo_slice = list(days[valid_end:test_start])
        test_slice = list(days[test_start:test_end])
        folds.append(
            {
                "train_days": train_slice,
                "purge_days": purge_slice,
                "valid_days": valid_slice,
                "embargo_days": embargo_slice,
                "test_days": test_slice,
            }
        )
        start += step_days
    return folds


def _rows_for_days(df: pd.DataFrame, day_list: Sequence[str]) -> pd.DataFrame:
    mask = df["trade_date"].astype(str).isin(set(str(x) for x in day_list))
    return df.loc[mask].sort_values("timestamp").copy()


def _aggregate_metrics(records: List[Dict[str, Optional[float]]]) -> Dict[str, Optional[float]]:
    if not records:
        return {}
    keys = sorted(set().union(*[set(r.keys()) for r in records]))
    out: Dict[str, Optional[float]] = {}
    for key in keys:
        vals = [r.get(key) for r in records]
        numeric = [float(v) for v in vals if v is not None and np.isfinite(v)]
        if not numeric:
            out[f"{key}_mean"] = None
            out[f"{key}_std"] = None
            continue
        out[f"{key}_mean"] = float(np.mean(numeric))
        out[f"{key}_std"] = float(np.std(numeric))
    return out


def _train_eval_fold(
    side_df: pd.DataFrame,
    feature_columns: Sequence[str],
    target_col: str,
    fold: Dict[str, List[str]],
    config: TrainConfig,
) -> Dict[str, object]:
    train_df = _rows_for_days(side_df, fold["train_days"])
    valid_df = _rows_for_days(side_df, fold["valid_days"])
    test_df = _rows_for_days(side_df, fold["test_days"])

    if len(train_df) == 0 or len(valid_df) == 0 or len(test_df) == 0:
        return {
            "fold_ok": False,
            "error": "empty split partition in fold",
            "days": fold,
        }

    x_train = train_df.loc[:, list(feature_columns)]
    y_train = train_df[target_col].astype(int).to_numpy()
    x_valid = valid_df.loc[:, list(feature_columns)]
    y_valid = valid_df[target_col].astype(int).to_numpy()
    x_test = test_df.loc[:, list(feature_columns)]
    y_test = test_df[target_col].astype(int).to_numpy()

    model = build_baseline_pipeline(config)
    model.fit(x_train, y_train)
    valid_prob = model.predict_proba(x_valid)[:, 1]
    test_prob = model.predict_proba(x_test)[:, 1]

    return {
        "fold_ok": True,
        "days": fold,
        "rows": {
            "train": int(len(train_df)),
            "valid": int(len(valid_df)),
            "test": int(len(test_df)),
        },
        "metrics": {
            "valid": compute_metrics(y_valid, valid_prob),
            "test": compute_metrics(y_test, test_prob),
        },
    }


def _run_side_walk_forward(
    df: pd.DataFrame,
    side: str,
    feature_columns: Sequence[str],
    config: TrainConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    purge_days: int,
    embargo_days: int,
) -> Dict[str, object]:
    target_col = f"{side}_label"
    valid_col = f"{side}_label_valid"
    work = df[(df[valid_col] == 1.0) & df[target_col].notna()].copy()
    work[target_col] = work[target_col].astype(int)
    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work = work.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    days = _to_sorted_days(work)
    folds = build_day_folds(
        days=days,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        purge_days=purge_days,
        embargo_days=embargo_days,
    )
    fold_results: List[Dict[str, object]] = []
    valid_metrics: List[Dict[str, Optional[float]]] = []
    test_metrics: List[Dict[str, Optional[float]]] = []
    for fold in folds:
        fold_result = _train_eval_fold(work, feature_columns, target_col, fold, config)
        fold_results.append(fold_result)
        if fold_result.get("fold_ok"):
            valid_metrics.append(fold_result["metrics"]["valid"])
            test_metrics.append(fold_result["metrics"]["test"])

    return {
        "rows_total": int(len(work)),
        "days_total": int(len(days)),
        "fold_count": int(len(folds)),
        "folds": fold_results,
        "aggregate": {
            "valid": _aggregate_metrics(valid_metrics),
            "test": _aggregate_metrics(test_metrics),
        },
    }


def run_walk_forward(
    labeled_df: pd.DataFrame,
    config: TrainConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    purge_days: int = 0,
    embargo_days: int = 0,
    feature_profile: str = FEATURE_PROFILE_ALL,
) -> Dict[str, object]:
    frame = labeled_df.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    feature_columns = select_feature_columns(frame, feature_profile=feature_profile)
    if not feature_columns:
        raise ValueError("no feature columns available for walk-forward")

    ce = _run_side_walk_forward(
        frame,
        side="ce",
        feature_columns=feature_columns,
        config=config,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        purge_days=purge_days,
        embargo_days=embargo_days,
    )
    pe = _run_side_walk_forward(
        frame,
        side="pe",
        feature_columns=feature_columns,
        config=config,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        purge_days=purge_days,
        embargo_days=embargo_days,
    )

    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": int(len(frame)),
        "feature_count": int(len(feature_columns)),
        "feature_columns": feature_columns,
        "feature_profile": str(feature_profile),
        "train_config": asdict(config),
        "walk_forward_config": {
            "train_days": int(train_days),
            "valid_days": int(valid_days),
            "test_days": int(test_days),
            "step_days": int(step_days),
            "purge_days": int(purge_days),
            "embargo_days": int(embargo_days),
        },
        "ce": ce,
        "pe": pe,
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward validation for CE/PE models")
    parser.add_argument(
        "--labeled-data",
        default="ml_pipeline/artifacts/t05_labeled_features.parquet",
        help="Labeled feature parquet input",
    )
    parser.add_argument(
        "--report-out",
        default="ml_pipeline/artifacts/t07_walk_forward_report.json",
        help="Walk-forward report output JSON",
    )
    parser.add_argument("--train-days", type=int, default=3, help="Number of train days per fold")
    parser.add_argument("--valid-days", type=int, default=1, help="Number of validation days per fold")
    parser.add_argument("--test-days", type=int, default=1, help="Number of test days per fold")
    parser.add_argument("--step-days", type=int, default=1, help="Fold shift in days")
    parser.add_argument("--purge-days", type=int, default=0, help="Gap days between train and valid windows")
    parser.add_argument("--embargo-days", type=int, default=0, help="Gap days between valid and test windows")
    parser.add_argument(
        "--feature-profile",
        default=FEATURE_PROFILE_ALL,
        choices=list(FEATURE_PROFILES),
        help="Feature set profile",
    )
    parser.add_argument("--random-state", type=int, default=None, help="Random seed")
    parser.add_argument("--max-depth", type=int, default=None, help="XGBoost max depth")
    parser.add_argument("--n-estimators", type=int, default=None, help="XGBoost tree count")
    parser.add_argument("--learning-rate", type=float, default=None, help="XGBoost learning rate")
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    if not labeled_path.exists():
        print(f"ERROR: labeled dataset not found: {labeled_path}")
        return 2

    default_cfg = TrainConfig()
    cfg = TrainConfig(
        train_ratio=default_cfg.train_ratio,
        valid_ratio=default_cfg.valid_ratio,
        random_state=args.random_state if args.random_state is not None else default_cfg.random_state,
        max_depth=args.max_depth if args.max_depth is not None else default_cfg.max_depth,
        n_estimators=args.n_estimators if args.n_estimators is not None else default_cfg.n_estimators,
        learning_rate=args.learning_rate if args.learning_rate is not None else default_cfg.learning_rate,
    )

    df = pd.read_parquet(labeled_path)
    report = run_walk_forward(
        labeled_df=df,
        config=cfg,
        train_days=args.train_days,
        valid_days=args.valid_days,
        test_days=args.test_days,
        step_days=args.step_days,
        purge_days=args.purge_days,
        embargo_days=args.embargo_days,
        feature_profile=str(args.feature_profile),
    )
    report_out = Path(args.report_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Input rows: {len(df)}")
    print(f"Features used: {report['feature_count']}")
    print(f"CE folds: {report['ce']['fold_count']}")
    print(f"PE folds: {report['pe']['fold_count']}")
    print(f"Report: {report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
