import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from .config import DecisionConfig, TrainConfig
from .train_baseline import build_baseline_pipeline, select_feature_columns
from .walk_forward import build_day_folds


def threshold_values(min_value: float, max_value: float, step: float) -> List[float]:
    if step <= 0:
        raise ValueError("step must be > 0")
    if max_value < min_value:
        raise ValueError("max_value must be >= min_value")
    values = np.arange(min_value, max_value + (step * 0.5), step)
    return [float(round(v, 10)) for v in values]


def _rows_for_days(df: pd.DataFrame, day_list: Sequence[str]) -> pd.DataFrame:
    mask = df["trade_date"].astype(str).isin(set(str(x) for x in day_list))
    return df.loc[mask].sort_values("timestamp").copy()


def _prepare_side_data(df: pd.DataFrame, side: str) -> pd.DataFrame:
    target_col = f"{side}_label"
    valid_col = f"{side}_label_valid"
    ret_col = f"{side}_forward_return"
    out = df[(df[valid_col] == 1.0) & df[target_col].notna() & df[ret_col].notna()].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    out[target_col] = out[target_col].astype(int)
    return out


def build_fold_predictions(
    labeled_df: pd.DataFrame,
    side: str,
    feature_columns: Sequence[str],
    config: TrainConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
) -> List[Dict[str, object]]:
    side_df = _prepare_side_data(labeled_df, side)
    days = sorted(side_df["trade_date"].astype(str).unique().tolist())
    folds = build_day_folds(days, train_days=train_days, valid_days=valid_days, test_days=test_days, step_days=step_days)
    target_col = f"{side}_label"
    ret_col = f"{side}_forward_return"

    results: List[Dict[str, object]] = []
    for fold in folds:
        train_df = _rows_for_days(side_df, fold["train_days"])
        valid_df = _rows_for_days(side_df, fold["valid_days"])
        test_df = _rows_for_days(side_df, fold["test_days"])
        if len(train_df) == 0 or len(valid_df) == 0 or len(test_df) == 0:
            results.append(
                {
                    "fold_ok": False,
                    "days": fold,
                    "error": "empty split partition",
                }
            )
            continue

        y_train = train_df[target_col].to_numpy()
        classes = np.unique(y_train)
        if len(classes) < 2:
            constant_prob = float(classes[0]) if len(classes) == 1 else 0.0
            valid_prob = np.full(len(valid_df), constant_prob, dtype=float)
            test_prob = np.full(len(test_df), constant_prob, dtype=float)
        else:
            model = build_baseline_pipeline(config)
            model.fit(train_df.loc[:, list(feature_columns)], y_train)
            valid_prob = model.predict_proba(valid_df.loc[:, list(feature_columns)])[:, 1]
            test_prob = model.predict_proba(test_df.loc[:, list(feature_columns)])[:, 1]

        results.append(
            {
                "fold_ok": True,
                "days": fold,
                "valid": {
                    "prob": valid_prob,
                    "ret": valid_df[ret_col].to_numpy(dtype=float),
                    "label": valid_df[target_col].to_numpy(dtype=int),
                },
                "test": {
                    "prob": test_prob,
                    "ret": test_df[ret_col].to_numpy(dtype=float),
                    "label": test_df[target_col].to_numpy(dtype=int),
                },
            }
        )
    return results


def evaluate_threshold(prob: np.ndarray, forward_ret: np.ndarray, threshold: float, cost_per_trade: float) -> Dict[str, float]:
    prob = np.asarray(prob, dtype=float)
    forward_ret = np.asarray(forward_ret, dtype=float)
    trade_mask = prob >= float(threshold)
    n = len(prob)
    trades = int(trade_mask.sum())
    if trades == 0:
        return {
            "threshold": float(threshold),
            "rows": int(n),
            "trades": 0,
            "trade_rate": 0.0,
            "mean_net_per_trade": 0.0,
            "total_net_return": 0.0,
            "mean_net_per_row": 0.0,
        }
    gross = forward_ret[trade_mask]
    net = gross - float(cost_per_trade)
    total_net = float(np.sum(net))
    mean_net_per_trade = float(np.mean(net))
    mean_net_per_row = float(total_net / float(n)) if n > 0 else 0.0
    return {
        "threshold": float(threshold),
        "rows": int(n),
        "trades": trades,
        "trade_rate": float(trades / n) if n > 0 else 0.0,
        "mean_net_per_trade": mean_net_per_trade,
        "total_net_return": total_net,
        "mean_net_per_row": mean_net_per_row,
    }


def _concat_fold_partition(folds: Sequence[Dict[str, object]], partition: str) -> Dict[str, np.ndarray]:
    probs: List[np.ndarray] = []
    returns: List[np.ndarray] = []
    labels: List[np.ndarray] = []
    for fold in folds:
        if not fold.get("fold_ok"):
            continue
        payload = fold[partition]
        probs.append(np.asarray(payload["prob"], dtype=float))
        returns.append(np.asarray(payload["ret"], dtype=float))
        labels.append(np.asarray(payload["label"], dtype=int))
    if not probs:
        return {"prob": np.array([], dtype=float), "ret": np.array([], dtype=float), "label": np.array([], dtype=int)}
    return {
        "prob": np.concatenate(probs),
        "ret": np.concatenate(returns),
        "label": np.concatenate(labels),
    }


def find_best_threshold(
    folds: Sequence[Dict[str, object]],
    thresholds: Sequence[float],
    cost_per_trade: float,
) -> Dict[str, object]:
    valid_data = _concat_fold_partition(folds, "valid")
    if len(valid_data["prob"]) == 0:
        return {
            "best_threshold": None,
            "search_metric": "mean_net_per_trade",
            "grid": [],
            "best_valid": None,
            "valid_summary": None,
        }

    grid_rows: List[Dict[str, float]] = []
    for thr in thresholds:
        row = evaluate_threshold(valid_data["prob"], valid_data["ret"], threshold=float(thr), cost_per_trade=cost_per_trade)
        grid_rows.append(row)

    # Maximize mean net return per trade; deterministic tie-break by higher trade count then lower threshold.
    ordered = sorted(
        grid_rows,
        key=lambda x: (x["mean_net_per_trade"], x["trades"], -x["threshold"]),
        reverse=True,
    )
    best = ordered[0]
    return {
        "best_threshold": float(best["threshold"]),
        "search_metric": "mean_net_per_trade",
        "grid": grid_rows,
        "best_valid": best,
        "valid_summary": {
            "rows": int(len(valid_data["prob"])),
            "positive_rate": float(np.mean(valid_data["label"])) if len(valid_data["label"]) else 0.0,
        },
    }


def evaluate_chosen_threshold_on_test(
    folds: Sequence[Dict[str, object]],
    threshold: float,
    cost_per_trade: float,
) -> Dict[str, object]:
    test_data = _concat_fold_partition(folds, "test")
    stats = evaluate_threshold(test_data["prob"], test_data["ret"], threshold=threshold, cost_per_trade=cost_per_trade)
    out = {
        "threshold": float(threshold),
        "test_summary": {
            "rows": int(len(test_data["prob"])),
            "positive_rate": float(np.mean(test_data["label"])) if len(test_data["label"]) else 0.0,
        },
        "test_metrics": stats,
    }
    return out


def optimize_side_threshold(
    labeled_df: pd.DataFrame,
    side: str,
    feature_columns: Sequence[str],
    train_config: TrainConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    threshold_min: float,
    threshold_max: float,
    threshold_step: float,
    cost_per_trade: float,
) -> Dict[str, object]:
    folds = build_fold_predictions(
        labeled_df=labeled_df,
        side=side,
        feature_columns=feature_columns,
        config=train_config,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
    )
    grid = threshold_values(threshold_min, threshold_max, threshold_step)
    search = find_best_threshold(folds, thresholds=grid, cost_per_trade=cost_per_trade)
    if search["best_threshold"] is None:
        return {
            "fold_count": int(len(folds)),
            "fold_ok_count": int(sum(1 for f in folds if f.get("fold_ok"))),
            "selected_threshold": None,
            "search": search,
            "test_eval": None,
        }
    test_eval = evaluate_chosen_threshold_on_test(
        folds=folds,
        threshold=float(search["best_threshold"]),
        cost_per_trade=cost_per_trade,
    )
    return {
        "fold_count": int(len(folds)),
        "fold_ok_count": int(sum(1 for f in folds if f.get("fold_ok"))),
        "selected_threshold": float(search["best_threshold"]),
        "search": search,
        "test_eval": test_eval,
    }


def run_threshold_optimization(
    labeled_df: pd.DataFrame,
    train_config: TrainConfig,
    decision_config: DecisionConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
) -> Dict[str, object]:
    frame = labeled_df.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    feature_columns = select_feature_columns(frame)
    if not feature_columns:
        raise ValueError("no feature columns available for threshold optimization")

    ce = optimize_side_threshold(
        labeled_df=frame,
        side="ce",
        feature_columns=feature_columns,
        train_config=train_config,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        threshold_min=decision_config.threshold_min,
        threshold_max=decision_config.threshold_max,
        threshold_step=decision_config.threshold_step,
        cost_per_trade=decision_config.cost_per_trade,
    )
    pe = optimize_side_threshold(
        labeled_df=frame,
        side="pe",
        feature_columns=feature_columns,
        train_config=train_config,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        threshold_min=decision_config.threshold_min,
        threshold_max=decision_config.threshold_max,
        threshold_step=decision_config.threshold_step,
        cost_per_trade=decision_config.cost_per_trade,
    )

    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": int(len(frame)),
        "feature_count": int(len(feature_columns)),
        "feature_columns": feature_columns,
        "train_config": asdict(train_config),
        "decision_config": asdict(decision_config),
        "walk_forward_config": {
            "train_days": int(train_days),
            "valid_days": int(valid_days),
            "test_days": int(test_days),
            "step_days": int(step_days),
        },
        "ce": ce,
        "pe": pe,
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Optimize CE/PE probability thresholds via walk-forward validation")
    parser.add_argument(
        "--labeled-data",
        default="ml_pipeline/artifacts/t05_labeled_features.parquet",
        help="Labeled feature parquet input",
    )
    parser.add_argument(
        "--report-out",
        default="ml_pipeline/artifacts/t08_threshold_report.json",
        help="Threshold optimization report output",
    )
    parser.add_argument("--train-days", type=int, default=3)
    parser.add_argument("--valid-days", type=int, default=1)
    parser.add_argument("--test-days", type=int, default=1)
    parser.add_argument("--step-days", type=int, default=1)
    parser.add_argument("--threshold-min", type=float, default=None)
    parser.add_argument("--threshold-max", type=float, default=None)
    parser.add_argument("--threshold-step", type=float, default=None)
    parser.add_argument("--cost-per-trade", type=float, default=None)
    parser.add_argument("--random-state", type=int, default=None)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--n-estimators", type=int, default=None)
    parser.add_argument("--learning-rate", type=float, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    if not labeled_path.exists():
        print(f"ERROR: labeled dataset not found: {labeled_path}")
        return 2

    default_train = TrainConfig()
    train_cfg = TrainConfig(
        train_ratio=default_train.train_ratio,
        valid_ratio=default_train.valid_ratio,
        random_state=args.random_state if args.random_state is not None else default_train.random_state,
        max_depth=args.max_depth if args.max_depth is not None else default_train.max_depth,
        n_estimators=args.n_estimators if args.n_estimators is not None else default_train.n_estimators,
        learning_rate=args.learning_rate if args.learning_rate is not None else default_train.learning_rate,
    )
    default_decision = DecisionConfig()
    decision_cfg = DecisionConfig(
        threshold_min=args.threshold_min if args.threshold_min is not None else default_decision.threshold_min,
        threshold_max=args.threshold_max if args.threshold_max is not None else default_decision.threshold_max,
        threshold_step=args.threshold_step if args.threshold_step is not None else default_decision.threshold_step,
        cost_per_trade=args.cost_per_trade if args.cost_per_trade is not None else default_decision.cost_per_trade,
    )

    df = pd.read_parquet(labeled_path)
    report = run_threshold_optimization(
        labeled_df=df,
        train_config=train_cfg,
        decision_config=decision_cfg,
        train_days=args.train_days,
        valid_days=args.valid_days,
        test_days=args.test_days,
        step_days=args.step_days,
    )

    report_out = Path(args.report_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Input rows: {len(df)}")
    print(f"Features used: {report['feature_count']}")
    print(f"CE threshold: {report['ce']['selected_threshold']}")
    print(f"PE threshold: {report['pe']['selected_threshold']}")
    print(f"Report: {report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
