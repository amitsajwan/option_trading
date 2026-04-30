from __future__ import annotations

import pandas as pd

from strategy_app.tools.deterministic_research_suite import (
    aggregate_suite_profile_results,
    build_suite_recommendation,
    default_suite_scenarios,
)
from strategy_app.tools.deterministic_profile_tournament import _normalize_export_profile_trade_ids


def test_default_suite_scenarios_include_primary_and_anchor_layers() -> None:
    scenarios = default_suite_scenarios(
        date_from="2023-01-01",
        date_to="2024-03-28",
        anchor_date_from="2022-01-01",
    )
    assert [item.name for item in scenarios] == [
        "primary_monthly",
        "primary_quarterly",
        "primary_full",
        "anchor_quarterly",
        "anchor_full",
    ]
    assert scenarios[0].date_from == "2023-01-01"
    assert scenarios[-1].date_from == "2022-01-01"


def test_aggregate_suite_profile_results_prefers_more_wins_then_rank() -> None:
    primary = pd.DataFrame(
        [
            {"profile_id": "det_prod_v1", "profile_label": "Prod", "avg_return_pct": 0.01, "median_return_pct": 0.01, "profitable_window_pct": 0.8, "beat_baseline_pct": 0.8, "avg_profit_factor": 1.2, "avg_win_rate": 0.6, "avg_drawdown_pct": -0.01, "worst_window_return_pct": -0.01, "worst_drawdown_pct": -0.02, "total_trades": 12},
            {"profile_id": "det_v3_v1", "profile_label": "V3", "avg_return_pct": 0.005, "median_return_pct": 0.004, "profitable_window_pct": 0.6, "beat_baseline_pct": 0.4, "avg_profit_factor": 1.1, "avg_win_rate": 0.55, "avg_drawdown_pct": -0.02, "worst_window_return_pct": -0.02, "worst_drawdown_pct": -0.03, "total_trades": 30},
        ]
    )
    anchor = pd.DataFrame(
        [
            {"profile_id": "det_v3_v1", "profile_label": "V3", "avg_return_pct": 0.02, "median_return_pct": 0.018, "profitable_window_pct": 0.9, "beat_baseline_pct": 0.7, "avg_profit_factor": 1.3, "avg_win_rate": 0.62, "avg_drawdown_pct": -0.015, "worst_window_return_pct": -0.01, "worst_drawdown_pct": -0.025, "total_trades": 24},
            {"profile_id": "det_prod_v1", "profile_label": "Prod", "avg_return_pct": 0.01, "median_return_pct": 0.009, "profitable_window_pct": 0.7, "beat_baseline_pct": 0.5, "avg_profit_factor": 1.15, "avg_win_rate": 0.58, "avg_drawdown_pct": -0.012, "worst_window_return_pct": -0.015, "worst_drawdown_pct": -0.02, "total_trades": 14},
        ]
    )
    full = pd.DataFrame(
        [
            {"profile_id": "det_prod_v1", "profile_label": "Prod", "avg_return_pct": 0.012, "median_return_pct": 0.012, "profitable_window_pct": 1.0, "beat_baseline_pct": 1.0, "avg_profit_factor": 1.25, "avg_win_rate": 0.61, "avg_drawdown_pct": -0.009, "worst_window_return_pct": 0.012, "worst_drawdown_pct": -0.009, "total_trades": 10},
            {"profile_id": "det_v3_v1", "profile_label": "V3", "avg_return_pct": 0.011, "median_return_pct": 0.011, "profitable_window_pct": 1.0, "beat_baseline_pct": 0.0, "avg_profit_factor": 1.22, "avg_win_rate": 0.6, "avg_drawdown_pct": -0.011, "worst_window_return_pct": 0.011, "worst_drawdown_pct": -0.011, "total_trades": 22},
        ]
    )
    combined = aggregate_suite_profile_results(
        {
            "primary_quarterly": primary,
            "anchor_quarterly": anchor,
            "anchor_full": full,
        }
    )
    assert combined.iloc[0]["profile_id"] == "det_prod_v1"
    assert combined.iloc[0]["scenario_wins"] == 2
    assert combined.iloc[0]["avg_rank"] < combined.iloc[1]["avg_rank"]


def test_build_suite_recommendation_returns_top_profile_and_winning_scenarios() -> None:
    leaderboard = pd.DataFrame(
        [
            {
                "profile_id": "det_prod_v1",
                "profile_label": "Prod",
                "scenarios": 5,
                "scenario_wins": 3,
                "scenario_podiums": 5,
                "avg_rank": 1.4,
                "avg_return_pct": 0.01,
                "avg_profitable_window_pct": 0.8,
                "avg_beat_baseline_pct": 0.6,
            }
        ]
    )
    scenario_recommendations = pd.DataFrame(
        [
            {"scenario_name": "primary_quarterly", "recommended_profile_id": "det_prod_v1"},
            {"scenario_name": "anchor_full", "recommended_profile_id": "det_prod_v1"},
            {"scenario_name": "primary_full", "recommended_profile_id": "det_v3_v1"},
        ]
    )
    recommendation = build_suite_recommendation(leaderboard, scenario_recommendations)
    assert recommendation["status"] == "ok"
    assert recommendation["recommended_profile_id"] == "det_prod_v1"
    assert recommendation["winning_scenarios"] == ["primary_quarterly", "anchor_full"]


def test_normalize_export_profile_trade_ids_accepts_spaces_and_commas() -> None:
    assert _normalize_export_profile_trade_ids(["det_prod_v1,det_v3_v1", "det_prod_v1", " det_setup_v1 "]) == [
        "det_prod_v1",
        "det_v3_v1",
        "det_setup_v1",
    ]
