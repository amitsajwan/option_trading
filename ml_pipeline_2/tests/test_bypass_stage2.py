"""Tests for bypass_stage2 pipeline modifications.

These tests validate that when bypass_stage2=True:
1. Dummy stage2 probabilities are injected correctly
2. Dual-side execution produces both CE and PE trades
3. Policy evaluation functions accept dual_side_mode parameter
4. The combined pipeline runs without errors
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


@pytest.fixture
def dummy_key_columns() -> list[str]:
    return ["trade_date", "timestamp", "snapshot_id"]


@pytest.fixture
def dummy_scores(dummy_key_columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": ["2024-01-01", "2024-01-01", "2024-01-01"],
            "timestamp": [1000, 1001, 1002],
            "snapshot_id": ["a", "b", "c"],
            "stage1_entry_prob": [0.6, 0.7, 0.8],
        }
    )


@pytest.fixture
def dummy_utility(dummy_key_columns: list[str]) -> pd.DataFrame:
    rows = []
    for i in range(3):
        for recipe_id in ["L0", "L3"]:
            rows.append(
                {
                    "trade_date": "2024-01-01",
                    "timestamp": 1000 + i,
                    "snapshot_id": chr(ord("a") + i),
                    "recipe_id": recipe_id,
                    "side": "CE",
                    "net_return": 0.01 * (i + 1),
                }
            )
            rows.append(
                {
                    "trade_date": "2024-01-01",
                    "timestamp": 1000 + i,
                    "snapshot_id": chr(ord("a") + i),
                    "recipe_id": recipe_id,
                    "side": "PE",
                    "net_return": -0.005 * (i + 1),
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture
def dummy_stage3_scores(dummy_key_columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": ["2024-01-01", "2024-01-01", "2024-01-01"],
            "timestamp": [1000, 1001, 1002],
            "snapshot_id": ["a", "b", "c"],
            "recipe_prob_L0": [0.5, 0.6, 0.4],
            "recipe_prob_L3": [0.5, 0.4, 0.6],
            "L0__ce_net_return": [0.01, 0.02, 0.015],
            "L0__pe_net_return": [-0.005, -0.01, -0.007],
            "L3__ce_net_return": [0.02, 0.01, 0.03],
            "L3__pe_net_return": [-0.01, -0.005, -0.02],
        }
    )


def test_score_stage2_package_bypass() -> None:
    """Test that _score_stage2_package returns dummy neutral probs when _bypass_stage2 is set."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.pipeline import _score_stage2_package

    frame = pd.DataFrame(
        {
            "trade_date": ["2024-01-01"],
            "timestamp": [1000],
            "snapshot_id": ["a"],
        }
    )
    bypass_package = {"_bypass_stage2": True, "prediction_mode": "direction"}
    result = _score_stage2_package(frame, bypass_package)
    assert len(result) == 1
    assert result["direction_up_prob"].iloc[0] == 0.5
    assert result["direction_trade_prob"].iloc[0] == 1.0
    assert result["ce_prob"].iloc[0] == 0.5
    assert result["pe_prob"].iloc[0] == 0.5


def test_evaluate_combined_policy_dual_side_mode() -> None:
    """Test that dual_side_mode produces both CE and PE trades."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.pipeline import _evaluate_combined_policy

    base_frame = pd.DataFrame(
        {
            "trade_date": ["2024-01-01", "2024-01-01", "2024-01-01"],
            "timestamp": [1000, 1001, 1002],
            "snapshot_id": ["a", "b", "c"],
            "recipe_prob_L0": [0.5, 0.6, 0.4],
            "recipe_prob_L3": [0.5, 0.4, 0.6],
            "L0__ce_net_return": [0.01, 0.02, 0.015],
            "L0__pe_net_return": [-0.005, -0.01, -0.007],
            "L3__ce_net_return": [0.02, 0.01, 0.03],
            "L3__pe_net_return": [-0.01, -0.005, -0.02],
            "direction_up_prob": [0.5, 0.5, 0.5],
            "direction_trade_prob": [1.0, 1.0, 1.0],
        }
    )
    utility = pd.DataFrame(
        {
            "trade_date": ["2024-01-01", "2024-01-01", "2024-01-01"],
            "timestamp": [1000, 1001, 1002],
            "snapshot_id": ["a", "b", "c"],
            "direction_up_prob": [0.5, 0.5, 0.5],
        }
    )
    stage1_scores = pd.DataFrame(
        {
            "trade_date": ["2024-01-01", "2024-01-01", "2024-01-01"],
            "timestamp": [1000, 1001, 1002],
            "snapshot_id": ["a", "b", "c"],
            "entry_prob": [0.6, 0.7, 0.8],
            "stage1_entry_prob": [0.6, 0.7, 0.8],
        }
    )
    stage2_scores = pd.DataFrame(
        {
            "trade_date": ["2024-01-01", "2024-01-01", "2024-01-01"],
            "timestamp": [1000, 1001, 1002],
            "snapshot_id": ["a", "b", "c"],
            "direction_up_prob": [0.5, 0.5, 0.5],
            "direction_trade_prob": [1.0, 1.0, 1.0],
            "stage2_direction_up_prob": [0.5, 0.5, 0.5],
            "stage2_direction_trade_prob": [1.0, 1.0, 1.0],
        }
    )
    stage3_scores = base_frame.copy()
    stage3_scores["direction_up_prob"] = [0.5, 0.5, 0.5]

    # Single-side mode (default)
    single_summary = _evaluate_combined_policy(
        utility,
        stage1_scores,
        stage2_scores,
        stage3_scores,
        stage1_threshold=0.5,
        stage2_policy={"policy_id": "direction_gate_threshold_v1", "selected_trade_threshold": 0.5, "selected_ce_threshold": 0.55, "selected_pe_threshold": 0.55, "selected_min_edge": 0.05},
        recipe_ids=["L0", "L3"],
        recipe_threshold=0.5,
        recipe_margin_min=0.02,
    )
    assert isinstance(single_summary, dict)
    assert "trades" in single_summary

    # Dual-side mode
    dual_summary = _evaluate_combined_policy(
        utility,
        stage1_scores,
        stage2_scores,
        stage3_scores,
        stage1_threshold=0.5,
        stage2_policy={"policy_id": "direction_gate_threshold_v1", "selected_trade_threshold": 0.5, "selected_ce_threshold": 0.55, "selected_pe_threshold": 0.55, "selected_min_edge": 0.05},
        recipe_ids=["L0", "L3"],
        recipe_threshold=0.5,
        recipe_margin_min=0.02,
        dual_side_mode=True,
    )
    assert isinstance(dual_summary, dict)
    assert "trades" in dual_summary
    # In dual side mode with neutral direction probs and threshold 0.5,
    # both CE and PE should be selected, so trades should be >= single side
    assert dual_summary["trades"] >= single_summary["trades"]


def test_create_bypass_stage2_result() -> None:
    """Test that _create_bypass_stage2_result produces valid dummy artifacts."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.pipeline import _create_bypass_stage2_result

    class FakeCtx:
        def __init__(self) -> None:
            self.output_root = Path(tempfile.mkdtemp())

        def write_json(self, path: str, data: Any) -> Path:
            target = self.output_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data, default=str), encoding="utf-8")
            return target

    ctx = FakeCtx()
    stage_frames = {
        "stage2": {
            "research_valid": pd.DataFrame(
                {
                    "trade_date": ["2024-01-01"],
                    "timestamp": [1000],
                    "snapshot_id": ["a"],
                }
            ),
            "final_holdout": pd.DataFrame(
                {
                    "trade_date": ["2024-01-01"],
                    "timestamp": [1001],
                    "snapshot_id": ["b"],
                }
            ),
        }
    }
    result = _create_bypass_stage2_result(ctx, stage_frames)
    assert result["search_package"]["_bypass_stage2"] is True
    assert result["model_package"]["_bypass_stage2"] is True
    assert len(result["validation_scores"]) == 1
    assert result["validation_scores"]["direction_up_prob"].iloc[0] == 0.5
    assert len(result["holdout_scores"]) == 1
    assert result["holdout_scores"]["direction_up_prob"].iloc[0] == 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
