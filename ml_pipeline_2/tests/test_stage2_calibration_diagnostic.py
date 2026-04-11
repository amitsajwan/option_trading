from __future__ import annotations

from pathlib import Path

import pandas as pd

from ml_pipeline_2.contracts.types import LabelRecipe
from ml_pipeline_2.staged.pipeline import _merge_policy_inputs
from ml_pipeline_2.staged import stage2_calibration as s2c
from ml_pipeline_2.staged.stage2_diagnostic_common import Stage2DiagnosticContext


def test_stage2_calibration_diagnostic_writes_summary_and_selects_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "completed_run"
    run_dir.mkdir(parents=True)

    support = pd.DataFrame(
        {
            "trade_date": ["2024-01-01"] * 4 + ["2024-02-01"] * 4,
            "timestamp": pd.date_range("2024-01-01", periods=8, freq="h"),
            "snapshot_id": [f"s{i}" for i in range(8)],
            "split": ["research_valid"] * 4 + ["final_holdout"] * 4,
        }
    )

    oracle = support.loc[:, ["trade_date", "timestamp", "snapshot_id", "split"]].copy()
    oracle["entry_label"] = [1] * 8
    oracle["direction_label"] = ["CE", "PE", "CE", "PE", "CE", "PE", "CE", "PE"]
    oracle["direction_up"] = [1, 0, 1, 0, 1, 0, 1, 0]
    oracle["recipe_label"] = ["L6"] * 8
    oracle["best_net_return_after_cost"] = [0.02, 0.02, 0.015, 0.015, 0.018, 0.017, 0.014, 0.014]
    oracle["best_ce_net_return_after_cost"] = [0.02, -0.01, 0.015, -0.01, 0.018, -0.01, 0.014, -0.01]
    oracle["best_pe_net_return_after_cost"] = [-0.01, 0.02, -0.01, 0.015, -0.01, 0.017, -0.01, 0.014]
    oracle["direction_return_edge_after_cost"] = [0.03, 0.03, 0.025, 0.025, 0.028, 0.027, 0.024, 0.024]

    utility = support.loc[:, ["trade_date", "timestamp", "snapshot_id", "split"]].copy()
    utility["L3__ce_net_return"] = oracle["best_ce_net_return_after_cost"]
    utility["L3__pe_net_return"] = oracle["best_pe_net_return_after_cost"]
    utility["L6__ce_net_return"] = [0.022, -0.01, 0.017, -0.01, 0.020, -0.01, 0.016, -0.01]
    utility["L6__pe_net_return"] = [-0.01, 0.022, -0.01, 0.017, -0.01, 0.019, -0.01, 0.016]
    utility["best_ce_net_return_after_cost"] = oracle["best_ce_net_return_after_cost"]
    utility["best_pe_net_return_after_cost"] = oracle["best_pe_net_return_after_cost"]
    utility["best_available_net_return_after_cost"] = oracle["best_net_return_after_cost"]
    utility_base = utility.drop(columns=["best_ce_net_return_after_cost", "best_pe_net_return_after_cost"])

    stage1_scores = support.loc[:, ["trade_date", "timestamp", "snapshot_id"]].copy()
    stage1_scores["entry_prob"] = [0.8] * 8

    stage2_scores = support.loc[:, ["trade_date", "timestamp", "snapshot_id"]].copy()
    stage2_scores["direction_trade_prob"] = [0.8] * 8
    stage2_scores["direction_up_prob"] = [0.70, 0.40, 0.58, 0.45, 0.68, 0.42, 0.57, 0.46]

    recipes = [
        LabelRecipe(recipe_id="L3", horizon_minutes=20, take_profit_pct=0.0025, stop_loss_pct=0.0010),
        LabelRecipe(recipe_id="L6", horizon_minutes=25, take_profit_pct=0.0025, stop_loss_pct=0.0010),
    ]

    def fake_window(frame: pd.DataFrame, window: dict) -> pd.DataFrame:
        split_col = "split"
        if split_col not in frame.columns:
            for candidate in ("split_x", "split_y"):
                if candidate in frame.columns:
                    split_col = candidate
                    break
        return frame.loc[frame[split_col].eq(str(window["name"]))].reset_index(drop=True)

    def fake_score_single_target(frame: pd.DataFrame, package, *, prob_col: str) -> pd.DataFrame:
        return stage1_scores.loc[stage1_scores["snapshot_id"].isin(frame["snapshot_id"])].reset_index(drop=True)

    def fake_score_stage2_package(frame: pd.DataFrame, package) -> pd.DataFrame:
        return stage2_scores.loc[stage2_scores["snapshot_id"].isin(frame["snapshot_id"])].reset_index(drop=True)

    summary = {
        "policy_reports": {
            "stage1": {"selected_threshold": 0.50},
            "stage2": {
                "policy_id": "direction_gate_threshold_v1",
                "selected_trade_threshold": 0.50,
                "selected_ce_threshold": 0.60,
                "selected_pe_threshold": 0.60,
                "selected_min_edge": 0.0,
            },
        }
    }
    resolved_config = {"windows": {"research_valid": {"name": "research_valid"}, "final_holdout": {"name": "final_holdout"}}}
    ctx = Stage2DiagnosticContext(
        source_run_dir=run_dir,
        source_run_id="stage2_calibration_smoke",
        summary=summary,
        resolved_config=resolved_config,
        fixed_recipe_ids=("L3", "L6"),
        recipe_universe=recipes,
        parquet_root=tmp_path,
        support_context=support.copy(),
        runtime_block_expiry=False,
        diagnostic_windows={
            "research_valid": fake_window(_merge_policy_inputs(oracle.copy(), utility_base.copy()), {"name": "research_valid"}),
            "final_holdout": fake_window(_merge_policy_inputs(oracle.copy(), utility_base.copy()), {"name": "final_holdout"}),
        },
        stage1_package={"kind": "stage1"},
        stage2_package={"kind": "stage2"},
        stage1_policy=summary["policy_reports"]["stage1"],
        stage2_policy=summary["policy_reports"]["stage2"],
        stage1_filtered=support.copy(),
        stage2_filtered=support.copy(),
    )

    def fake_build_stage2_scored_window_frame(context: Stage2DiagnosticContext, *, window_name: str, include_stage2_feature_columns=None) -> pd.DataFrame:
        diagnostic_window = context.diagnostic_windows[window_name]
        return _merge_policy_inputs(
            diagnostic_window,
            fake_score_single_target(diagnostic_window, None, prob_col="entry_prob"),
            fake_score_stage2_package(diagnostic_window, None),
        )

    monkeypatch.setattr(s2c, "load_stage2_diagnostic_context", lambda **kwargs: ctx)
    monkeypatch.setattr(s2c, "build_stage2_scored_window_frame", fake_build_stage2_scored_window_frame)

    payload = s2c.run_stage2_calibration_diagnostic(
        run_dir=run_dir,
        fixed_recipe_ids=("L3", "L6"),
        trade_threshold_grid=(0.50,),
        ce_threshold_grid=(0.55, 0.60),
        pe_threshold_grid=(0.55, 0.60),
        min_edge_grid=(0.0,),
        validation_policy={"validation_min_trades_soft": 1},
    )

    assert payload["analysis_kind"] == "stage2_calibration_diagnostic_v1"
    assert payload["source_run_id"] == "stage2_calibration_smoke"
    assert payload["winner"]["validation_selected_recipe_id"] == "L6"
    assert payload["winner"]["ce_threshold"] == 0.55
    assert payload["winner"]["pe_threshold"] == 0.55
    assert payload["winner"]["validation"]["actionable"]["selected_vs_oracle_agreement"] == 1.0
    assert payload["winner"]["holdout"]["fixed_recipe_summaries"]["L6"]["net_return_sum"] > 0.0
    assert Path(payload["paths"]["stage2_calibration_summary"]).exists()
