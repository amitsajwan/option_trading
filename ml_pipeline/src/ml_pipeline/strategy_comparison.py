import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from .backtest_engine import run_backtest
from .config import DecisionConfig, TrainConfig


def load_threshold_payload(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_thresholds(payload: Dict[str, object]) -> Tuple[float, float]:
    ce = payload.get("ce", {}).get("selected_threshold")
    pe = payload.get("pe", {}).get("selected_threshold")
    if ce is None or pe is None:
        raise ValueError("threshold payload missing selected_threshold for ce/pe")
    return float(ce), float(pe)


def train_config_from_payload(payload: Dict[str, object], fallback: TrainConfig) -> TrainConfig:
    cfg = payload.get("train_config") or {}
    return TrainConfig(
        train_ratio=float(cfg.get("train_ratio", fallback.train_ratio)),
        valid_ratio=float(cfg.get("valid_ratio", fallback.valid_ratio)),
        random_state=int(cfg.get("random_state", fallback.random_state)),
        max_depth=int(cfg.get("max_depth", fallback.max_depth)),
        n_estimators=int(cfg.get("n_estimators", fallback.n_estimators)),
        learning_rate=float(cfg.get("learning_rate", fallback.learning_rate)),
    )


def walk_forward_config_from_payload(payload: Dict[str, object]) -> Dict[str, int]:
    wf = payload.get("walk_forward_config") or {}
    return {
        "train_days": int(wf.get("train_days", 3)),
        "valid_days": int(wf.get("valid_days", 1)),
        "test_days": int(wf.get("test_days", 1)),
        "step_days": int(wf.get("step_days", 1)),
    }


def cost_values_from_input(cost_grid: Optional[str], default_cost: float) -> List[float]:
    if not cost_grid:
        return [float(default_cost)]
    values: List[float] = []
    for part in cost_grid.split(","):
        raw = part.strip().lower()
        if not raw:
            continue
        if raw == "default":
            values.append(float(default_cost))
            continue
        values.append(float(raw))
    uniq = sorted(set(values))
    return uniq if uniq else [float(default_cost)]


def _mode_thresholds(mode: str, ce_threshold: float, pe_threshold: float) -> Tuple[float, float]:
    # Probabilities are in [0,1]. Threshold=2.0 disables side cleanly.
    if mode == "ce_only":
        return float(ce_threshold), 2.0
    if mode == "pe_only":
        return 2.0, float(pe_threshold)
    if mode == "dual":
        return float(ce_threshold), float(pe_threshold)
    raise ValueError(f"unsupported mode: {mode}")


def _extract_summary(report: Dict[str, object]) -> Dict[str, object]:
    keep = [
        "trades_total",
        "test_rows_total",
        "trade_rate",
        "ce_trades",
        "pe_trades",
        "gross_return_sum",
        "net_return_sum",
        "mean_net_return_per_trade",
        "win_rate",
        "max_drawdown",
        "fold_count",
        "fold_ok_count",
    ]
    return {k: report[k] for k in keep}


def run_strategy_comparison(
    labeled_df: pd.DataFrame,
    ce_threshold: float,
    pe_threshold: float,
    cost_values: Sequence[float],
    train_config: TrainConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
) -> Dict[str, object]:
    modes = ["ce_only", "pe_only", "dual"]
    results: Dict[str, Dict[str, object]] = {}

    for mode in modes:
        mode_results: Dict[str, object] = {}
        ce_thr, pe_thr = _mode_thresholds(mode, ce_threshold, pe_threshold)
        for cost in cost_values:
            _, report = run_backtest(
                labeled_df=labeled_df,
                ce_threshold=ce_thr,
                pe_threshold=pe_thr,
                cost_per_trade=float(cost),
                train_config=train_config,
                train_days=train_days,
                valid_days=valid_days,
                test_days=test_days,
                step_days=step_days,
            )
            mode_results[str(cost)] = {
                "mode": mode,
                "cost_per_trade": float(cost),
                "ce_threshold": float(ce_thr),
                "pe_threshold": float(pe_thr),
                "summary": _extract_summary(report),
            }
        results[mode] = mode_results

    # Consistency check: all strategies must evaluate same test_rows_total and fold_count.
    baseline_rows = None
    baseline_folds = None
    for mode in modes:
        sample_cost_key = str(cost_values[0])
        summary = results[mode][sample_cost_key]["summary"]
        rows = int(summary["test_rows_total"])
        folds = int(summary["fold_count"])
        if baseline_rows is None:
            baseline_rows = rows
            baseline_folds = folds
        else:
            if rows != baseline_rows or folds != baseline_folds:
                raise ValueError("strategy comparison inconsistency: evaluation dataset differs across modes")

    # Pick best mode by net_return_sum at default (first) cost.
    default_cost_key = str(cost_values[0])
    ranking = []
    for mode in modes:
        s = results[mode][default_cost_key]["summary"]
        ranking.append(
            {
                "mode": mode,
                "net_return_sum": float(s["net_return_sum"]),
                "mean_net_return_per_trade": float(s["mean_net_return_per_trade"]),
                "max_drawdown": float(s["max_drawdown"]),
                "trade_rate": float(s["trade_rate"]),
            }
        )
    ranking.sort(key=lambda x: x["net_return_sum"], reverse=True)

    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "train_config": asdict(train_config),
        "walk_forward_config": {
            "train_days": int(train_days),
            "valid_days": int(valid_days),
            "test_days": int(test_days),
            "step_days": int(step_days),
        },
        "thresholds": {
            "ce": float(ce_threshold),
            "pe": float(pe_threshold),
        },
        "cost_values": [float(x) for x in cost_values],
        "results": results,
        "ranking_default_cost": ranking,
        "best_mode_default_cost": ranking[0]["mode"] if ranking else None,
        "consistency_check": {
            "test_rows_total": int(baseline_rows) if baseline_rows is not None else 0,
            "fold_count": int(baseline_folds) if baseline_folds is not None else 0,
        },
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare CE-only, PE-only, and dual strategy modes")
    parser.add_argument(
        "--labeled-data",
        default="ml_pipeline/artifacts/t05_labeled_features.parquet",
        help="Labeled data parquet",
    )
    parser.add_argument(
        "--threshold-report",
        default="ml_pipeline/artifacts/t08_threshold_report.json",
        help="Threshold optimization report",
    )
    parser.add_argument(
        "--report-out",
        default="ml_pipeline/artifacts/t10_strategy_comparison_report.json",
        help="Strategy comparison report JSON",
    )
    parser.add_argument(
        "--cost-grid",
        default=None,
        help="Comma-separated costs. Example: default,0.001,0.002",
    )
    parser.add_argument("--train-days", type=int, default=None)
    parser.add_argument("--valid-days", type=int, default=None)
    parser.add_argument("--test-days", type=int, default=None)
    parser.add_argument("--step-days", type=int, default=None)
    parser.add_argument("--ce-threshold", type=float, default=None)
    parser.add_argument("--pe-threshold", type=float, default=None)
    parser.add_argument("--random-state", type=int, default=None)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--n-estimators", type=int, default=None)
    parser.add_argument("--learning-rate", type=float, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    threshold_path = Path(args.threshold_report)
    if not labeled_path.exists():
        print(f"ERROR: labeled data not found: {labeled_path}")
        return 2
    if not threshold_path.exists():
        print(f"ERROR: threshold report not found: {threshold_path}")
        return 2

    payload = load_threshold_payload(threshold_path)
    ce_thr, pe_thr = resolve_thresholds(payload)
    if args.ce_threshold is not None:
        ce_thr = float(args.ce_threshold)
    if args.pe_threshold is not None:
        pe_thr = float(args.pe_threshold)

    default_train = TrainConfig()
    train_cfg = train_config_from_payload(payload, fallback=default_train)
    if args.random_state is not None:
        train_cfg = TrainConfig(
            train_ratio=train_cfg.train_ratio,
            valid_ratio=train_cfg.valid_ratio,
            random_state=int(args.random_state),
            max_depth=int(args.max_depth if args.max_depth is not None else train_cfg.max_depth),
            n_estimators=int(args.n_estimators if args.n_estimators is not None else train_cfg.n_estimators),
            learning_rate=float(args.learning_rate if args.learning_rate is not None else train_cfg.learning_rate),
        )
    else:
        train_cfg = TrainConfig(
            train_ratio=train_cfg.train_ratio,
            valid_ratio=train_cfg.valid_ratio,
            random_state=train_cfg.random_state,
            max_depth=int(args.max_depth if args.max_depth is not None else train_cfg.max_depth),
            n_estimators=int(args.n_estimators if args.n_estimators is not None else train_cfg.n_estimators),
            learning_rate=float(args.learning_rate if args.learning_rate is not None else train_cfg.learning_rate),
        )

    wf = walk_forward_config_from_payload(payload)
    train_days = int(args.train_days) if args.train_days is not None else wf["train_days"]
    valid_days = int(args.valid_days) if args.valid_days is not None else wf["valid_days"]
    test_days = int(args.test_days) if args.test_days is not None else wf["test_days"]
    step_days = int(args.step_days) if args.step_days is not None else wf["step_days"]

    default_cost = float((payload.get("decision_config") or {}).get("cost_per_trade", DecisionConfig().cost_per_trade))
    costs = cost_values_from_input(args.cost_grid, default_cost=default_cost)

    labeled = pd.read_parquet(labeled_path)
    report = run_strategy_comparison(
        labeled_df=labeled,
        ce_threshold=ce_thr,
        pe_threshold=pe_thr,
        cost_values=costs,
        train_config=train_cfg,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
    )

    out = Path(args.report_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Input rows: {len(labeled)}")
    print(f"Best mode (default cost): {report['best_mode_default_cost']}")
    print(f"Cost values: {report['cost_values']}")
    print(f"Consistency test_rows: {report['consistency_check']['test_rows_total']}")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())

