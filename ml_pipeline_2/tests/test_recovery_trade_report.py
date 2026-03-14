from __future__ import annotations

from pathlib import Path

import pandas as pd

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_research
from ml_pipeline_2.run_recovery_trade_report import build_recovery_trade_report
from ml_pipeline_2.tests.helpers import build_recovery_smoke_manifest, build_synthetic_feature_frames


def test_recovery_trade_report_writes_trade_level_outputs(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    run_dir = Path(summary["output_root"])

    report = build_recovery_trade_report(
        run_dir=run_dir,
        threshold=0.50,
    )

    trades_csv = Path(str(report["paths"]["trades_csv"]))
    trades_parquet = Path(str(report["paths"]["trades_parquet"]))
    exit_summary_csv = Path(str(report["paths"]["exit_reason_summary_csv"]))
    daily_summary_csv = Path(str(report["paths"]["daily_summary_csv"]))
    assert report["status"] == "completed"
    assert trades_csv.exists()
    assert trades_parquet.exists()
    assert exit_summary_csv.exists()
    assert daily_summary_csv.exists()
    assert report["threshold"] == 0.50
    assert "time_stop" in report["outcome_rules"]

    trades = pd.read_parquet(trades_parquet)
    assert len(trades) > 0
    assert {
        "decision_ts",
        "entry_ts",
        "planned_exit_ts",
        "event_end_ts",
        "chosen_side",
        "exit_reason",
        "gross_return",
        "net_return_after_cost",
        "net_outcome",
    } <= set(trades.columns)
    assert trades["chosen_side"].isin(["CE", "PE"]).all()

