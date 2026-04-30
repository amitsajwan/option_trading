from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ml_pipeline_2.staged import stage2_feature_signal as s2f
from ml_pipeline_2.staged.pipeline import _merge_policy_inputs
from ml_pipeline_2.staged.stage2_diagnostic_common import Stage2DiagnosticContext


class _DummyEstimator:
    def __init__(self, coef: list[float]) -> None:
        self.coef_ = np.asarray([coef], dtype=float)


class _DummyPipeline:
    def __init__(self, estimator: object) -> None:
        self.steps = [("model", estimator)]


def test_stage2_feature_signal_diagnostic_writes_summary_and_memo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "completed_run"
    run_dir.mkdir(parents=True)

    support = pd.DataFrame(
        {
            "trade_date": ["2024-01-01"] * 10 + ["2024-02-01"] * 10,
            "timestamp": pd.date_range("2024-01-01", periods=20, freq="h"),
            "snapshot_id": [f"s{i}" for i in range(20)],
            "split": ["research_valid"] * 10 + ["final_holdout"] * 10,
        }
    )

    oracle = support.loc[:, ["trade_date", "timestamp", "snapshot_id", "split"]].copy()
    oracle["entry_label"] = [1] * 20
    oracle["direction_label"] = ["CE"] * 5 + ["PE"] * 5 + ["CE"] * 5 + ["PE"] * 5
    oracle["best_ce_net_return_after_cost"] = [0.02] * 20
    oracle["best_pe_net_return_after_cost"] = [0.02] * 20

    utility = support.loc[:, ["trade_date", "timestamp", "snapshot_id", "split"]].copy()

    merged_features = support.loc[:, ["trade_date", "timestamp", "snapshot_id", "split"]].copy()
    merged_features["entry_prob"] = [0.8] * 20
    merged_features["f1"] = [3.0] * 5 + [0.0] * 5 + [3.1] * 5 + [0.1] * 5
    merged_features["f2"] = [0.1] * 5 + [2.8] * 5 + [0.2] * 5 + [2.9] * 5
    merged_features["f3"] = [1.5] * 5 + [0.3] * 5 + [1.4] * 5 + [0.4] * 5

    stage2_package = {
        "direction_package": {
            "feature_columns": ["f1", "f2", "f3"],
            "models": {"direction": _DummyPipeline(_DummyEstimator([0.9, -0.7, 0.4]))},
        }
    }

    def fake_window(frame: pd.DataFrame, window: dict) -> pd.DataFrame:
        split_col = "split"
        if split_col not in frame.columns:
            for candidate in ("split_x", "split_y"):
                if candidate in frame.columns:
                    split_col = candidate
                    break
        return frame.loc[frame[split_col].eq(str(window["name"]))].reset_index(drop=True)

    summary = {"policy_reports": {"stage1": {"selected_threshold": 0.50}}}
    resolved_config = {"windows": {"research_valid": {"name": "research_valid"}, "final_holdout": {"name": "final_holdout"}}}
    diagnostic_base = _merge_policy_inputs(oracle.copy(), utility.copy())
    ctx = Stage2DiagnosticContext(
        source_run_dir=run_dir,
        source_run_id="stage2_feature_signal_smoke",
        summary=summary,
        resolved_config=resolved_config,
        fixed_recipe_ids=("L3", "L6"),
        recipe_universe=["L3", "L6"],
        parquet_root=tmp_path,
        support_context=support.copy(),
        runtime_block_expiry=False,
        diagnostic_windows={
            "research_valid": fake_window(diagnostic_base, {"name": "research_valid"}),
            "final_holdout": fake_window(diagnostic_base, {"name": "final_holdout"}),
        },
        stage1_package={"kind": "stage1"},
        stage2_package=stage2_package,
        stage1_policy=summary["policy_reports"]["stage1"],
        stage2_policy={},
        stage1_filtered=support.copy(),
        stage2_filtered=support.copy(),
    )

    def fake_build_stage2_scored_window_frame(context: Stage2DiagnosticContext, *, window_name: str, include_stage2_feature_columns=None) -> pd.DataFrame:
        diagnostic_window = context.diagnostic_windows[window_name]
        return _merge_policy_inputs(
            diagnostic_window,
            merged_features.loc[merged_features["snapshot_id"].isin(diagnostic_window["snapshot_id"])].reset_index(drop=True),
        )

    monkeypatch.setattr(s2f, "load_stage2_diagnostic_context", lambda **kwargs: ctx)
    monkeypatch.setattr(s2f, "build_stage2_scored_window_frame", fake_build_stage2_scored_window_frame)

    payload = s2f.run_stage2_feature_signal_diagnostic(
        run_dir=run_dir,
        min_stable_features=3,
    )

    assert payload["analysis_kind"] == "stage2_feature_signal_diagnostic_v1"
    assert payload["source_run_id"] == "stage2_feature_signal_smoke"
    assert payload["signal_exists"] is True
    assert payload["stable_feature_count"] == 3
    assert Path(payload["paths"]["stage2_feature_signal_summary"]).exists()
    assert Path(payload["paths"]["stage2_feature_signal_memo"]).exists()
