from __future__ import annotations

import pytest

from ml_pipeline_2.staged import stage2_policy_core as core


def test_normalize_grid_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        core._normalize_grid(())
    with pytest.raises(ValueError, match="within \\[0.0, 1.0\\]"):
        core._normalize_grid((0.1, 1.1))
    with pytest.raises(ValueError, match="within \\[0.0, 1.0\\]"):
        core._normalize_grid((0.1, float("nan")))


def test_is_current_stage2_policy_handles_trade_gate_and_non_trade_gate() -> None:
    trade_gate_policy = {
        "selected_trade_threshold": 0.45,
        "selected_ce_threshold": 0.60,
        "selected_pe_threshold": 0.65,
        "selected_min_edge": 0.05,
    }
    assert core._is_current_stage2_policy(
        trade_gate_policy,
        uses_trade_gate=True,
        trade_threshold=0.45,
        ce_threshold=0.60,
        pe_threshold=0.65,
        min_edge=0.05,
    )
    assert not core._is_current_stage2_policy(
        trade_gate_policy,
        uses_trade_gate=True,
        trade_threshold=0.50,
        ce_threshold=0.60,
        pe_threshold=0.65,
        min_edge=0.05,
    )

    non_trade_gate_policy = {
        "selected_ce_threshold": 0.55,
        "selected_pe_threshold": 0.55,
        "selected_min_edge": 0.10,
    }
    assert core._is_current_stage2_policy(
        non_trade_gate_policy,
        uses_trade_gate=False,
        trade_threshold=None,
        ce_threshold=0.55,
        pe_threshold=0.55,
        min_edge=0.10,
    )
    assert not core._is_current_stage2_policy(
        non_trade_gate_policy,
        uses_trade_gate=False,
        trade_threshold=None,
        ce_threshold=0.60,
        pe_threshold=0.55,
        min_edge=0.10,
    )


def test_best_recipe_id_uses_ranked_recipe_summary() -> None:
    policy_config = dict(core.DEFAULT_STAGE2_CALIBRATION_POLICY)
    window_eval = {
        "fixed_recipe_summaries": {
            "L3": {
                "net_return_sum": 0.05,
                "profit_factor": 1.20,
                "trades": 20,
                "long_share": 0.50,
                "short_share": 0.50,
                "block_rate": 0.10,
            },
            "L6": {
                "net_return_sum": 0.09,
                "profit_factor": 1.50,
                "trades": 20,
                "long_share": 0.50,
                "short_share": 0.50,
                "block_rate": 0.10,
            },
        }
    }
    assert core._best_recipe_id(window_eval, policy_config=policy_config) == "L6"
