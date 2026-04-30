from __future__ import annotations

import json
from pathlib import Path

import joblib
import pandas as pd

from ml_pipeline_2.contracts.types import LabelRecipe
from ml_pipeline_2.staged import skew_diagnostic as sd


def test_stage12_skew_diagnostic_merges_oracle_labels_into_all_levels(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "completed_run"
    run_dir.mkdir(parents=True)
    model_dir = run_dir / "models"
    model_dir.mkdir()
    stage1_package_path = model_dir / "stage1.joblib"
    stage2_package_path = model_dir / "stage2.joblib"
    joblib.dump({"kind": "stage1"}, stage1_package_path)
    joblib.dump({"kind": "stage2"}, stage2_package_path)

    summary = {
        "status": "completed",
        "run_id": "skew_diag_smoke",
        "recipe_catalog_id": "midday_l3_adjacent_v1",
        "stage_artifacts": {
            "stage1": {"model_package_path": str(stage1_package_path)},
            "stage2": {"model_package_path": str(stage2_package_path)},
        },
        "component_ids": {
            "stage1": {"view_id": "stage1_view"},
            "stage2": {"view_id": "stage2_view"},
        },
        "policy_reports": {
            "stage1": {"selected_threshold": 0.60},
            "stage2": {
                "policy_id": "direction_gate_threshold_v1",
                "selected_trade_threshold": 0.50,
                "selected_ce_threshold": 0.55,
                "selected_pe_threshold": 0.55,
                "selected_min_edge": 0.0,
            },
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
            "trade_date": ["2024-01-01", "2024-01-01", "2024-02-01", "2024-02-01"],
            "timestamp": pd.date_range("2024-01-01", periods=4, freq="h"),
            "snapshot_id": ["v1", "v2", "h1", "h2"],
            "split": ["research_valid", "research_valid", "final_holdout", "final_holdout"],
        }
    )

    stage1_scores = support.loc[:, ["trade_date", "timestamp", "snapshot_id"]].copy()
    stage1_scores["entry_prob"] = [0.90, 0.70, 0.95, 0.65]
    stage1_scores["entry_label"] = [999, 999, 999, 999]
    stage1_scores["direction_label"] = ["BAD", "BAD", "BAD", "BAD"]

    stage2_scores = support.loc[:, ["trade_date", "timestamp", "snapshot_id"]].copy()
    stage2_scores["direction_trade_prob"] = [0.90, 0.80, 0.90, 0.80]
    stage2_scores["direction_up_prob"] = [0.80, 0.20, 0.80, 0.20]
    stage2_scores["entry_label"] = [888, 888, 888, 888]
    stage2_scores["direction_label"] = ["WORSE", "WORSE", "WORSE", "WORSE"]

    oracle = support.loc[:, ["trade_date", "timestamp", "snapshot_id", "split"]].copy()
    oracle["entry_label"] = [1, 1, 1, 1]
    oracle["direction_label"] = ["CE", "PE", "CE", "PE"]
    oracle["best_ce_net_return_after_cost"] = [0.020, -0.005, 0.018, -0.004]
    oracle["best_pe_net_return_after_cost"] = [-0.010, 0.012, -0.006, 0.011]

    utility = support.loc[:, ["trade_date", "timestamp", "snapshot_id", "split"]].copy()
    utility["L3__ce_net_return"] = [0.020, -0.005, 0.018, -0.004]
    utility["L3__pe_net_return"] = [-0.010, 0.012, -0.006, 0.011]
    utility["L6__ce_net_return"] = [0.019, -0.006, 0.017, -0.005]
    utility["L6__pe_net_return"] = [-0.009, 0.011, -0.005, 0.010]
    utility["best_ce_net_return_after_cost"] = oracle["best_ce_net_return_after_cost"]
    utility["best_pe_net_return_after_cost"] = oracle["best_pe_net_return_after_cost"]

    recipes = [
        LabelRecipe(recipe_id="L3", horizon_minutes=20, take_profit_pct=0.0025, stop_loss_pct=0.0010),
        LabelRecipe(recipe_id="L6", horizon_minutes=25, take_profit_pct=0.0025, stop_loss_pct=0.0010),
    ]

    def fake_load_dataset(parquet_root: Path, dataset_name: str) -> pd.DataFrame:
        if dataset_name == "support":
            return support.copy()
        if dataset_name == "stage1_ds":
            return support.copy()
        if dataset_name == "stage2_ds":
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

    monkeypatch.setattr(sd, "_load_dataset", fake_load_dataset)
    monkeypatch.setattr(sd, "_apply_runtime_filters", fake_apply_runtime_filters)
    monkeypatch.setattr(sd, "_window", fake_window)
    monkeypatch.setattr(sd, "_build_oracle_targets", lambda *args, **kwargs: (oracle.copy(), utility.copy()))
    monkeypatch.setattr(sd, "_resolve_recipe_universe", lambda **kwargs: recipes)
    monkeypatch.setattr(sd, "_score_single_target", lambda frame, package, prob_col: stage1_scores.loc[stage1_scores["snapshot_id"].isin(frame["snapshot_id"])].reset_index(drop=True))
    monkeypatch.setattr(sd, "_score_stage2_package", lambda frame, package: stage2_scores.loc[stage2_scores["snapshot_id"].isin(frame["snapshot_id"])].reset_index(drop=True))
    monkeypatch.setattr(sd, "view_registry", lambda: {"stage1_view": type("V", (), {"dataset_name": "stage1_ds"})(), "stage2_view": type("V", (), {"dataset_name": "stage2_ds"})()})

    payload = sd.run_stage12_skew_diagnostic(
        run_dir=run_dir,
        top_fractions=(0.5, 0.25),
        fixed_recipe_ids=("L3", "L6"),
    )

    valid = payload["research_valid"]
    holdout = payload["final_holdout"]

    assert valid["raw_oracle"]["oracle_direction"]["ce"] == 1
    assert valid["raw_oracle"]["oracle_direction"]["pe"] == 1
    assert holdout["raw_oracle"]["oracle_direction"]["ce"] == 1
    assert holdout["raw_oracle"]["oracle_direction"]["pe"] == 1
    assert valid["stage12_actionable"]["oracle_direction_for_selected"]["ce"] == 1
    assert valid["stage12_actionable"]["oracle_direction_for_selected"]["pe"] == 1
    assert valid["stage12_actionable"]["selected_vs_oracle_agreement"] == 1.0
    assert Path(payload["paths"]["skew_diagnostic"]).exists()
