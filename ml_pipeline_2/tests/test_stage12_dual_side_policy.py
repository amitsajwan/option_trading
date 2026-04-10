from __future__ import annotations

import json
from pathlib import Path

import joblib
import pandas as pd

from ml_pipeline_2.contracts.types import LabelRecipe
from ml_pipeline_2.staged import dual_side_policy as dsp


def test_stage12_dual_side_policy_selects_independent_side_mix(
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
        "run_id": "dual_side_policy_smoke",
        "recipe_catalog_id": "midday_l3_adjacent_v1",
        "stage_artifacts": {
            "stage1": {"model_package_path": str(stage1_package_path)},
            "stage2": {"model_package_path": str(stage2_package_path)},
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

    utility = pd.DataFrame(
        {
            "split": ["research_valid"] * 6 + ["final_holdout"] * 6,
            "timestamp": pd.date_range("2024-01-01", periods=12, freq="h"),
        }
    )

    def fake_load_dataset(parquet_root: Path, dataset_name: str) -> pd.DataFrame:
        assert dataset_name == "support"
        return utility.copy()

    def fake_apply_runtime_filters(frame: pd.DataFrame, **kwargs):
        return frame.copy(), {}

    def fake_window(frame: pd.DataFrame, window: dict) -> pd.DataFrame:
        return frame.loc[frame["split"].eq(str(window["name"]))].reset_index(drop=True)

    def fake_build_oracle_targets(frame: pd.DataFrame, recipes, cost_per_trade: float):
        return frame.copy(), frame.copy()

    recipes = [
        LabelRecipe(recipe_id="L3", horizon_minutes=20, take_profit_pct=0.0025, stop_loss_pct=0.0010),
        LabelRecipe(recipe_id="L6", horizon_minutes=25, take_profit_pct=0.0025, stop_loss_pct=0.0010),
    ]

    def fake_selected_stage12_trades_for_window(*, window_name: str, **kwargs) -> pd.DataFrame:
        if window_name == "research_valid":
            rows = [
                {"trade_date": "2024-01-01", "timestamp": "2024-01-01 10:00:00", "snapshot_id": "v1", "selected_side": "CE", "entry_prob": 0.8, "direction_trade_prob": 0.8, "direction_up_prob": 0.75, "selected_side_prob": 0.75, "ranking_score": 0.90, "oracle_selected_side_return": 0.018, "L3__ce_net_return": 0.012, "L3__pe_net_return": 0.0, "L6__ce_net_return": 0.014, "L6__pe_net_return": 0.0},
                {"trade_date": "2024-01-01", "timestamp": "2024-01-01 10:05:00", "snapshot_id": "v2", "selected_side": "CE", "entry_prob": 0.78, "direction_trade_prob": 0.79, "direction_up_prob": 0.72, "selected_side_prob": 0.72, "ranking_score": 0.82, "oracle_selected_side_return": 0.010, "L3__ce_net_return": -0.004, "L3__pe_net_return": 0.0, "L6__ce_net_return": -0.002, "L6__pe_net_return": 0.0},
                {"trade_date": "2024-01-01", "timestamp": "2024-01-01 10:10:00", "snapshot_id": "v3", "selected_side": "CE", "entry_prob": 0.75, "direction_trade_prob": 0.78, "direction_up_prob": 0.70, "selected_side_prob": 0.70, "ranking_score": 0.74, "oracle_selected_side_return": 0.009, "L3__ce_net_return": -0.003, "L3__pe_net_return": 0.0, "L6__ce_net_return": 0.000, "L6__pe_net_return": 0.0},
                {"trade_date": "2024-01-01", "timestamp": "2024-01-01 10:15:00", "snapshot_id": "v4", "selected_side": "PE", "entry_prob": 0.82, "direction_trade_prob": 0.80, "direction_up_prob": 0.20, "selected_side_prob": 0.80, "ranking_score": 0.88, "oracle_selected_side_return": 0.020, "L3__ce_net_return": 0.0, "L3__pe_net_return": 0.013, "L6__ce_net_return": 0.0, "L6__pe_net_return": 0.015},
                {"trade_date": "2024-01-01", "timestamp": "2024-01-01 10:20:00", "snapshot_id": "v5", "selected_side": "PE", "entry_prob": 0.80, "direction_trade_prob": 0.79, "direction_up_prob": 0.23, "selected_side_prob": 0.77, "ranking_score": 0.84, "oracle_selected_side_return": 0.017, "L3__ce_net_return": 0.0, "L3__pe_net_return": 0.011, "L6__ce_net_return": 0.0, "L6__pe_net_return": 0.014},
                {"trade_date": "2024-01-01", "timestamp": "2024-01-01 10:25:00", "snapshot_id": "v6", "selected_side": "PE", "entry_prob": 0.79, "direction_trade_prob": 0.78, "direction_up_prob": 0.24, "selected_side_prob": 0.76, "ranking_score": 0.81, "oracle_selected_side_return": 0.015, "L3__ce_net_return": 0.0, "L3__pe_net_return": 0.009, "L6__ce_net_return": 0.0, "L6__pe_net_return": 0.012},
            ]
        else:
            rows = [
                {"trade_date": "2024-02-01", "timestamp": "2024-02-01 10:00:00", "snapshot_id": "h1", "selected_side": "CE", "entry_prob": 0.78, "direction_trade_prob": 0.78, "direction_up_prob": 0.73, "selected_side_prob": 0.73, "ranking_score": 0.80, "oracle_selected_side_return": 0.012, "L3__ce_net_return": 0.010, "L3__pe_net_return": 0.0, "L6__ce_net_return": 0.011, "L6__pe_net_return": 0.0},
                {"trade_date": "2024-02-01", "timestamp": "2024-02-01 10:05:00", "snapshot_id": "h2", "selected_side": "CE", "entry_prob": 0.75, "direction_trade_prob": 0.77, "direction_up_prob": 0.71, "selected_side_prob": 0.71, "ranking_score": 0.72, "oracle_selected_side_return": -0.003, "L3__ce_net_return": -0.005, "L3__pe_net_return": 0.0, "L6__ce_net_return": -0.003, "L6__pe_net_return": 0.0},
                {"trade_date": "2024-02-01", "timestamp": "2024-02-01 10:10:00", "snapshot_id": "h3", "selected_side": "PE", "entry_prob": 0.83, "direction_trade_prob": 0.81, "direction_up_prob": 0.18, "selected_side_prob": 0.82, "ranking_score": 0.91, "oracle_selected_side_return": 0.018, "L3__ce_net_return": 0.0, "L3__pe_net_return": 0.012, "L6__ce_net_return": 0.0, "L6__pe_net_return": 0.014},
                {"trade_date": "2024-02-01", "timestamp": "2024-02-01 10:15:00", "snapshot_id": "h4", "selected_side": "PE", "entry_prob": 0.82, "direction_trade_prob": 0.80, "direction_up_prob": 0.21, "selected_side_prob": 0.79, "ranking_score": 0.86, "oracle_selected_side_return": 0.016, "L3__ce_net_return": 0.0, "L3__pe_net_return": 0.011, "L6__ce_net_return": 0.0, "L6__pe_net_return": 0.013},
                {"trade_date": "2024-02-01", "timestamp": "2024-02-01 10:20:00", "snapshot_id": "h5", "selected_side": "PE", "entry_prob": 0.80, "direction_trade_prob": 0.79, "direction_up_prob": 0.22, "selected_side_prob": 0.78, "ranking_score": 0.82, "oracle_selected_side_return": 0.014, "L3__ce_net_return": 0.0, "L3__pe_net_return": 0.010, "L6__ce_net_return": 0.0, "L6__pe_net_return": 0.012},
                {"trade_date": "2024-02-01", "timestamp": "2024-02-01 10:25:00", "snapshot_id": "h6", "selected_side": "PE", "entry_prob": 0.79, "direction_trade_prob": 0.78, "direction_up_prob": 0.24, "selected_side_prob": 0.76, "ranking_score": 0.79, "oracle_selected_side_return": -0.002, "L3__ce_net_return": 0.0, "L3__pe_net_return": -0.004, "L6__ce_net_return": 0.0, "L6__pe_net_return": -0.001},
            ]
        return pd.DataFrame(rows)

    monkeypatch.setattr(dsp, "_load_dataset", fake_load_dataset)
    monkeypatch.setattr(dsp, "_apply_runtime_filters", fake_apply_runtime_filters)
    monkeypatch.setattr(dsp, "_window", fake_window)
    monkeypatch.setattr(dsp, "_build_oracle_targets", fake_build_oracle_targets)
    monkeypatch.setattr(dsp, "_resolve_recipe_universe", lambda **kwargs: recipes)
    monkeypatch.setattr(dsp, "_selected_stage12_trades_for_window", fake_selected_stage12_trades_for_window)

    payload = dsp.run_stage12_dual_side_policy(
        run_dir=run_dir,
        ce_fraction_grid=(0.5, 1.0),
        pe_fraction_grid=(0.5, 1.0),
        fixed_recipe_ids=("L3", "L6"),
        validation_policy={"validation_min_trades_soft": 1},
    )

    assert payload["analysis_kind"] == "stage12_dual_side_policy_v1"
    assert payload["source_run_id"] == "dual_side_policy_smoke"
    assert payload["winner"]["recipe_id"] == "L6"
    assert payload["winner"]["validation"]["side_share_in_band"] is True
    assert payload["winner"]["holdout"]["side_share_in_band"] is True
    assert payload["winner"]["holdout"]["net_return_sum"] > 0.0
    assert Path(payload["paths"]["dual_side_policy_summary"]).exists()
