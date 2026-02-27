import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import pandas as pd

from .backtest_engine import run_backtest
from .config import DecisionConfig, TrainConfig
from .threshold_optimization import run_threshold_optimization
from .train_baseline import train_baseline_models
from .walk_forward import run_walk_forward


def _depth_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if str(c).startswith("depth_")]


def _variant_frame(df: pd.DataFrame, use_depth: bool) -> Tuple[pd.DataFrame, list[str]]:
    depth_cols = _depth_columns(df)
    if use_depth:
        return df.copy(), depth_cols
    return df.drop(columns=depth_cols, errors="ignore").copy(), depth_cols


def _extract_key_metrics(
    *,
    train_report: Dict[str, object],
    wf_report: Dict[str, object],
    th_report: Dict[str, object],
    bt_report: Optional[Dict[str, object]],
) -> Dict[str, object]:
    return {
        "feature_count": int(train_report.get("feature_count", 0)),
        "ce_valid_roc_auc": train_report.get("ce", {}).get("metrics", {}).get("valid", {}).get("roc_auc"),
        "pe_valid_roc_auc": train_report.get("pe", {}).get("metrics", {}).get("valid", {}).get("roc_auc"),
        "ce_wf_test_f1_mean": wf_report.get("ce", {}).get("aggregate", {}).get("test", {}).get("f1_mean"),
        "pe_wf_test_f1_mean": wf_report.get("pe", {}).get("aggregate", {}).get("test", {}).get("f1_mean"),
        "ce_selected_threshold": th_report.get("ce", {}).get("selected_threshold"),
        "pe_selected_threshold": th_report.get("pe", {}).get("selected_threshold"),
        "backtest_trades_total": bt_report.get("trades_total") if bt_report is not None else None,
        "backtest_net_return_sum": bt_report.get("net_return_sum") if bt_report is not None else None,
        "backtest_win_rate": bt_report.get("win_rate") if bt_report is not None else None,
    }


def run_depth_ablation(
    *,
    labeled_df: pd.DataFrame,
    train_config: TrainConfig,
    decision_config: DecisionConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
) -> Dict[str, object]:
    results: Dict[str, object] = {}
    for variant in ("baseline_no_depth", "with_depth"):
        use_depth = variant == "with_depth"
        variant_df, depth_cols = _variant_frame(labeled_df, use_depth=use_depth)
        has_depth = len(depth_cols) > 0 and any(c in variant_df.columns for c in depth_cols)
        if use_depth and not has_depth:
            results[variant] = {
                "status": "no_depth_columns",
                "depth_columns": depth_cols,
                "metrics": None,
            }
            continue

        train_report, _models = train_baseline_models(variant_df, config=train_config)
        wf_report = run_walk_forward(
            labeled_df=variant_df,
            config=train_config,
            train_days=train_days,
            valid_days=valid_days,
            test_days=test_days,
            step_days=step_days,
        )
        th_report = run_threshold_optimization(
            labeled_df=variant_df,
            train_config=train_config,
            decision_config=decision_config,
            train_days=train_days,
            valid_days=valid_days,
            test_days=test_days,
            step_days=step_days,
        )
        ce_thr = th_report.get("ce", {}).get("selected_threshold")
        pe_thr = th_report.get("pe", {}).get("selected_threshold")
        bt_report = None
        if ce_thr is not None and pe_thr is not None:
            _trades, bt_report = run_backtest(
                labeled_df=variant_df,
                ce_threshold=float(ce_thr),
                pe_threshold=float(pe_thr),
                cost_per_trade=float(decision_config.cost_per_trade),
                train_config=train_config,
                train_days=train_days,
                valid_days=valid_days,
                test_days=test_days,
                step_days=step_days,
            )

        results[variant] = {
            "status": "ok",
            "depth_columns": depth_cols,
            "metrics": _extract_key_metrics(
                train_report=train_report,
                wf_report=wf_report,
                th_report=th_report,
                bt_report=bt_report,
            ),
            "artifacts": {
                "train_report": train_report,
                "walk_forward_report": wf_report,
                "threshold_report": th_report,
                "backtest_report": bt_report,
            },
        }

    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": int(len(labeled_df)),
        "train_days": int(train_days),
        "valid_days": int(valid_days),
        "test_days": int(test_days),
        "step_days": int(step_days),
        "results": results,
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Depth ablation runner: baseline vs with-depth")
    parser.add_argument("--labeled-data", default="ml_pipeline/artifacts/t05_labeled_features.parquet")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t32_depth_ablation_report.json")
    parser.add_argument("--train-days", type=int, default=3)
    parser.add_argument("--valid-days", type=int, default=1)
    parser.add_argument("--test-days", type=int, default=1)
    parser.add_argument("--step-days", type=int, default=1)
    parser.add_argument("--cost-per-trade", type=float, default=0.0006)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--n-estimators", type=int, default=120)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    if not labeled_path.exists():
        print(f"ERROR: labeled dataset not found: {labeled_path}")
        return 2

    labeled_df = pd.read_parquet(labeled_path)
    train_cfg = TrainConfig(
        train_ratio=0.7,
        valid_ratio=0.15,
        random_state=int(args.random_state),
        max_depth=int(args.max_depth),
        n_estimators=int(args.n_estimators),
        learning_rate=float(args.learning_rate),
    )
    decision_cfg = DecisionConfig(
        threshold_min=0.50,
        threshold_max=0.90,
        threshold_step=0.01,
        cost_per_trade=float(args.cost_per_trade),
    )
    report = run_depth_ablation(
        labeled_df=labeled_df,
        train_config=train_cfg,
        decision_config=decision_cfg,
        train_days=int(args.train_days),
        valid_days=int(args.valid_days),
        test_days=int(args.test_days),
        step_days=int(args.step_days),
    )
    report_out = Path(args.report_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Rows: {len(labeled_df)}")
    print(f"Depth columns: {len(_depth_columns(labeled_df))}")
    print(f"Report: {report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
