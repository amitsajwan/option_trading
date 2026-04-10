from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ml_pipeline_2.staged import stage2_feature_signal as s2f


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

    summary = {
        "status": "completed",
        "run_id": "stage2_feature_signal_smoke",
        "recipe_catalog_id": "midday_l3_adjacent_v1",
        "stage_artifacts": {
            "stage1": {"model_package_path": str(run_dir / "stage1.joblib")},
            "stage2": {"model_package_path": str(run_dir / "stage2.joblib")},
        },
        "component_ids": {
            "stage1": {"view_id": "stage1_view"},
            "stage2": {"view_id": "stage2_view"},
        },
        "policy_reports": {
            "stage1": {"selected_threshold": 0.50},
        },
    }
    resolved_config = {
        "inputs": {
            "parquet_root": str(tmp_path / "parquet_root"),
            "support_dataset": "support",
        },
        "runtime": {"block_expiry": False},
        "training": {"cost_per_trade": 0.0},
        "windows": {
            "research_valid": {"name": "research_valid"},
            "final_holdout": {"name": "final_holdout"},
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "resolved_config.json").write_text(json.dumps(resolved_config), encoding="utf-8")

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

    def fake_joblib_load(path: str):
        if str(path).endswith("stage2.joblib"):
            return stage2_package
        return {"kind": "stage1"}

    def fake_load_dataset(parquet_root: Path, dataset_name: str) -> pd.DataFrame:
        if dataset_name == "support":
            return support.copy()
        raise AssertionError(dataset_name)

    def fake_apply_runtime_filters(frame: pd.DataFrame, **kwargs):
        return frame.copy(), {}

    def fake_window(frame: pd.DataFrame, window: dict) -> pd.DataFrame:
        split_col = "split"
        if split_col not in frame.columns:
            for candidate in ("split_x", "split_y"):
                if candidate in frame.columns:
                    split_col = candidate
                    break
        return frame.loc[frame[split_col].eq(str(window["name"]))].reset_index(drop=True)

    def fake_build_window_frame(**kwargs) -> pd.DataFrame:
        diagnostic_window = kwargs["diagnostic_window"]
        return s2f._merge_policy_inputs(  # type: ignore[attr-defined]
            diagnostic_window,
            merged_features.loc[merged_features["snapshot_id"].isin(diagnostic_window["snapshot_id"])].reset_index(drop=True),
        )

    monkeypatch.setattr(s2f.joblib, "load", fake_joblib_load)
    monkeypatch.setattr(s2f, "_load_dataset", fake_load_dataset)
    monkeypatch.setattr(s2f, "_apply_runtime_filters", fake_apply_runtime_filters)
    monkeypatch.setattr(s2f, "_resolve_recipe_universe", lambda **kwargs: ["L3", "L6"])
    monkeypatch.setattr(s2f, "_build_oracle_targets", lambda *args, **kwargs: (oracle.copy(), utility.copy()))
    monkeypatch.setattr(s2f, "_window", fake_window)
    monkeypatch.setattr(s2f, "_build_window_frame", fake_build_window_frame)

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
