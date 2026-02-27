import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .backtest_engine import run_backtest
from .config import DecisionConfig, TrainConfig


def load_threshold_payload(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_thresholds(payload: Dict[str, object]) -> tuple[float, float]:
    ce = payload.get("ce", {}).get("selected_threshold")
    pe = payload.get("pe", {}).get("selected_threshold")
    if ce is None or pe is None:
        raise ValueError("threshold payload missing ce/pe selected_threshold")
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


def _parse_float_grid(text: str) -> List[float]:
    values: List[float] = []
    for part in str(text).split(","):
        raw = part.strip()
        if not raw:
            continue
        values.append(float(raw))
    return sorted(set(values))


def _parse_str_grid(text: str) -> List[str]:
    values = [x.strip() for x in str(text).split(",") if x.strip()]
    uniq = sorted(set(values))
    return uniq


def _summary_fields(report: Dict[str, object]) -> Dict[str, object]:
    keys = [
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
        "execution_mode",
        "intrabar_tie_break",
        "slippage_per_trade",
        "exit_reason_counts",
    ]
    return {k: report[k] for k in keys if k in report}


def run_exit_policy_optimization(
    labeled_df: pd.DataFrame,
    ce_threshold: float,
    pe_threshold: float,
    cost_per_trade: float,
    train_config: TrainConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    tie_break_values: Sequence[str],
    slippage_values: Sequence[float],
    forced_eod_values: Sequence[str],
) -> Dict[str, object]:
    results: List[Dict[str, object]] = []
    base_rows = None
    base_folds = None

    for tie_break in tie_break_values:
        for slippage in slippage_values:
            for forced_eod in forced_eod_values:
                _, report = run_backtest(
                    labeled_df=labeled_df,
                    ce_threshold=ce_threshold,
                    pe_threshold=pe_threshold,
                    cost_per_trade=cost_per_trade,
                    train_config=train_config,
                    train_days=train_days,
                    valid_days=valid_days,
                    test_days=test_days,
                    step_days=step_days,
                    execution_mode="path_v2",
                    intrabar_tie_break=tie_break,
                    slippage_per_trade=float(slippage),
                    forced_eod_exit_time=str(forced_eod),
                )
                summary = _summary_fields(report)
                rows = int(summary["test_rows_total"])
                folds = int(summary["fold_count"])
                if base_rows is None:
                    base_rows = rows
                    base_folds = folds
                else:
                    if rows != base_rows or folds != base_folds:
                        raise ValueError("policy optimization inconsistency: test rows/folds changed across configs")

                results.append(
                    {
                        "config": {
                            "execution_mode": "path_v2",
                            "intrabar_tie_break": tie_break,
                            "slippage_per_trade": float(slippage),
                            "forced_eod_exit_time": str(forced_eod),
                        },
                        "summary": summary,
                    }
                )

    ranking = sorted(
        results,
        key=lambda x: (
            float(x["summary"]["net_return_sum"]),
            float(x["summary"]["mean_net_return_per_trade"]),
            -float(x["config"]["slippage_per_trade"]),
            1 if x["config"]["intrabar_tie_break"] == "tp" else 0,
        ),
        reverse=True,
    )
    best = ranking[0] if ranking else None

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
        "cost_per_trade": float(cost_per_trade),
        "search_space": {
            "intrabar_tie_break": list(tie_break_values),
            "slippage_per_trade": [float(x) for x in slippage_values],
            "forced_eod_exit_time": list(forced_eod_values),
        },
        "results": results,
        "ranking": ranking,
        "best_config": best["config"] if best else None,
        "best_summary": best["summary"] if best else None,
        "consistency_check": {
            "test_rows_total": int(base_rows) if base_rows is not None else 0,
            "fold_count": int(base_folds) if base_folds is not None else 0,
        },
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Optimize path-v2 exit policy configuration")
    parser.add_argument(
        "--labeled-data",
        default="ml_pipeline/artifacts/t05_labeled_features.parquet",
        help="Labeled data parquet",
    )
    parser.add_argument(
        "--threshold-report",
        default="ml_pipeline/artifacts/t08_threshold_report.json",
        help="Threshold report JSON",
    )
    parser.add_argument(
        "--report-out",
        default="ml_pipeline/artifacts/t18_exit_policy_optimization_report.json",
        help="Output optimization report JSON",
    )
    parser.add_argument("--tie-break-grid", default="sl,tp", help="Comma-separated tie-break values")
    parser.add_argument("--slippage-grid", default="0.0,0.0002,0.0005", help="Comma-separated slippage values")
    parser.add_argument("--forced-eod-grid", default="15:24", help="Comma-separated HH:MM values")
    parser.add_argument("--train-days", type=int, default=None)
    parser.add_argument("--valid-days", type=int, default=None)
    parser.add_argument("--test-days", type=int, default=None)
    parser.add_argument("--step-days", type=int, default=None)
    parser.add_argument("--ce-threshold", type=float, default=None)
    parser.add_argument("--pe-threshold", type=float, default=None)
    parser.add_argument("--cost-per-trade", type=float, default=None)
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
    wf = walk_forward_config_from_payload(payload)
    train_days = int(args.train_days) if args.train_days is not None else wf["train_days"]
    valid_days = int(args.valid_days) if args.valid_days is not None else wf["valid_days"]
    test_days = int(args.test_days) if args.test_days is not None else wf["test_days"]
    step_days = int(args.step_days) if args.step_days is not None else wf["step_days"]

    default_cost = float((payload.get("decision_config") or {}).get("cost_per_trade", DecisionConfig().cost_per_trade))
    cost_per_trade = float(args.cost_per_trade) if args.cost_per_trade is not None else default_cost

    tie_break_values = _parse_str_grid(args.tie_break_grid)
    slippage_values = _parse_float_grid(args.slippage_grid)
    forced_eod_values = _parse_str_grid(args.forced_eod_grid)
    if not tie_break_values or not slippage_values or not forced_eod_values:
        print("ERROR: empty search grid")
        return 2

    labeled = pd.read_parquet(labeled_path)
    report = run_exit_policy_optimization(
        labeled_df=labeled,
        ce_threshold=ce_thr,
        pe_threshold=pe_thr,
        cost_per_trade=cost_per_trade,
        train_config=train_cfg,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        tie_break_values=tie_break_values,
        slippage_values=slippage_values,
        forced_eod_values=forced_eod_values,
    )

    out = Path(args.report_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Configs tested: {len(report['results'])}")
    print(f"Best config: {report['best_config']}")
    print(f"Best net return sum: {report['best_summary']['net_return_sum'] if report['best_summary'] else None}")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
