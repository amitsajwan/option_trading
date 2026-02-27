import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd

from .backtest_engine import run_backtest
from .config import DecisionConfig, TrainConfig


def load_json(path: Path) -> Dict[str, object]:
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


def _default_profiles(default_cost: float) -> List[Dict[str, object]]:
    return [
        {
            "name": "fixed_horizon",
            "execution_mode": "fixed_horizon",
            "intrabar_tie_break": "sl",
            "slippage_per_trade": 0.0,
            "forced_eod_exit_time": "15:24",
            "cost_per_trade": float(default_cost),
        },
        {
            "name": "path_v2_default",
            "execution_mode": "path_v2",
            "intrabar_tie_break": "sl",
            "slippage_per_trade": 0.0002,
            "forced_eod_exit_time": "15:24",
            "cost_per_trade": float(default_cost),
        },
    ]


def _append_best_profile_from_t18(
    profiles: List[Dict[str, object]],
    t18_payload: Optional[Dict[str, object]],
    default_cost: float,
) -> List[Dict[str, object]]:
    if not t18_payload:
        return profiles
    best = t18_payload.get("best_config")
    if not isinstance(best, dict):
        return profiles
    candidate = {
        "name": "path_v2_best_t18",
        "execution_mode": str(best.get("execution_mode", "path_v2")),
        "intrabar_tie_break": str(best.get("intrabar_tie_break", "sl")),
        "slippage_per_trade": float(best.get("slippage_per_trade", 0.0)),
        "forced_eod_exit_time": str(best.get("forced_eod_exit_time", "15:24")),
        "cost_per_trade": float(default_cost),
    }
    for p in profiles:
        if (
            p["execution_mode"] == candidate["execution_mode"]
            and p["intrabar_tie_break"] == candidate["intrabar_tie_break"]
            and float(p["slippage_per_trade"]) == float(candidate["slippage_per_trade"])
            and p["forced_eod_exit_time"] == candidate["forced_eod_exit_time"]
            and float(p["cost_per_trade"]) == float(candidate["cost_per_trade"])
        ):
            return profiles
    return profiles + [candidate]


def run_strategy_comparison_v2(
    labeled_df: pd.DataFrame,
    ce_threshold: float,
    pe_threshold: float,
    train_config: TrainConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    profiles: List[Dict[str, object]],
) -> Dict[str, object]:
    results: List[Dict[str, object]] = []
    base_rows = None
    base_folds = None

    for profile in profiles:
        _, report = run_backtest(
            labeled_df=labeled_df,
            ce_threshold=ce_threshold,
            pe_threshold=pe_threshold,
            cost_per_trade=float(profile["cost_per_trade"]),
            train_config=train_config,
            train_days=train_days,
            valid_days=valid_days,
            test_days=test_days,
            step_days=step_days,
            execution_mode=str(profile["execution_mode"]),
            intrabar_tie_break=str(profile["intrabar_tie_break"]),
            slippage_per_trade=float(profile["slippage_per_trade"]),
            forced_eod_exit_time=str(profile["forced_eod_exit_time"]),
        )
        summary = _summary_fields(report)
        rows = int(summary["test_rows_total"])
        folds = int(summary["fold_count"])
        if base_rows is None:
            base_rows = rows
            base_folds = folds
        else:
            if rows != base_rows or folds != base_folds:
                raise ValueError("strategy comparison v2 inconsistency: evaluation dataset differs across profiles")

        results.append({"profile": profile, "summary": summary})

    ranking = sorted(
        results,
        key=lambda x: (
            float(x["summary"]["net_return_sum"]),
            float(x["summary"]["mean_net_return_per_trade"]),
            -float(x["profile"]["slippage_per_trade"]),
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
        "profiles": profiles,
        "results": results,
        "ranking": ranking,
        "best_profile": best["profile"] if best else None,
        "best_summary": best["summary"] if best else None,
        "consistency_check": {
            "test_rows_total": int(base_rows) if base_rows is not None else 0,
            "fold_count": int(base_folds) if base_folds is not None else 0,
        },
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare fixed-horizon vs dynamic exit policy profiles")
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
        "--t18-report",
        default="ml_pipeline/artifacts/t18_exit_policy_optimization_report.json",
        help="Optional T18 report to include best policy profile",
    )
    parser.add_argument(
        "--report-out",
        default="ml_pipeline/artifacts/t19_strategy_comparison_v2_report.json",
        help="Output comparison report JSON",
    )
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
    t18_path = Path(args.t18_report)
    if not labeled_path.exists():
        print(f"ERROR: labeled data not found: {labeled_path}")
        return 2
    if not threshold_path.exists():
        print(f"ERROR: threshold report not found: {threshold_path}")
        return 2

    payload = load_json(threshold_path)
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
    cost = float(args.cost_per_trade) if args.cost_per_trade is not None else default_cost
    profiles = _default_profiles(default_cost=cost)
    t18_payload = load_json(t18_path) if t18_path.exists() else None
    profiles = _append_best_profile_from_t18(profiles, t18_payload=t18_payload, default_cost=cost)

    labeled = pd.read_parquet(labeled_path)
    report = run_strategy_comparison_v2(
        labeled_df=labeled,
        ce_threshold=ce_thr,
        pe_threshold=pe_thr,
        train_config=train_cfg,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        profiles=profiles,
    )

    out = Path(args.report_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Profiles tested: {len(report['results'])}")
    print(f"Best profile: {report['best_profile']}")
    print(f"Best net return sum: {report['best_summary']['net_return_sum'] if report['best_summary'] else None}")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
