from __future__ import annotations

import pandas as pd

from strategy_app.tools.deterministic_profile_tournament import (
    aggregate_profile_results,
    build_calendar_windows,
    build_recommendation,
    default_profile_specs,
)
from strategy_app.tools.offline_strategy_analysis import MemorySignalLogger


def test_default_profile_specs_are_unique() -> None:
    profiles = default_profile_specs()
    ids = [item.profile_id for item in profiles]
    assert ids
    assert len(ids) == len(set(ids))
    assert "det_core_v2" in ids
    assert "det_prod_v1" in ids


def test_build_calendar_windows_monthly() -> None:
    windows = build_calendar_windows(
        ["2023-11-03", "2023-11-20", "2023-12-01", "2023-12-22", "2024-01-04"],
        "monthly",
    )
    assert [item.label for item in windows] == ["2023-11", "2023-12", "2024-01"]
    assert windows[0].date_from == "2023-11-03"
    assert windows[0].date_to == "2023-11-20"
    assert windows[2].date_from == "2024-01-04"
    assert windows[2].date_to == "2024-01-04"


def test_build_calendar_windows_quarterly() -> None:
    windows = build_calendar_windows(
        ["2023-11-03", "2023-12-22", "2024-01-04", "2024-03-19", "2024-04-02"],
        "quarterly",
    )
    assert [item.label for item in windows] == ["2023-Q4", "2024-Q1", "2024-Q2"]
    assert windows[1].date_from == "2024-01-04"
    assert windows[1].date_to == "2024-03-19"


def test_aggregate_profile_results_prefers_stability() -> None:
    frame = pd.DataFrame(
        [
            {
                "window_label": "2024-01",
                "profile_id": "det_core_v2",
                "profile_label": "Baseline",
                "trades": 4,
                "net_capital_return_pct": 0.01,
                "profit_factor": 1.2,
                "win_rate": 0.5,
                "max_drawdown_pct": -0.20,
                "profitable_window": True,
                "beat_baseline": False,
            },
            {
                "window_label": "2024-02",
                "profile_id": "det_core_v2",
                "profile_label": "Baseline",
                "trades": 5,
                "net_capital_return_pct": -0.02,
                "profit_factor": 0.9,
                "win_rate": 0.4,
                "max_drawdown_pct": -0.30,
                "profitable_window": False,
                "beat_baseline": False,
            },
            {
                "window_label": "2024-01",
                "profile_id": "det_orb_oi_combo_v1",
                "profile_label": "Combo",
                "trades": 6,
                "net_capital_return_pct": 0.03,
                "profit_factor": 1.5,
                "win_rate": 0.6,
                "max_drawdown_pct": -0.18,
                "profitable_window": True,
                "beat_baseline": True,
            },
            {
                "window_label": "2024-02",
                "profile_id": "det_orb_oi_combo_v1",
                "profile_label": "Combo",
                "trades": 6,
                "net_capital_return_pct": 0.01,
                "profit_factor": 1.3,
                "win_rate": 0.6,
                "max_drawdown_pct": -0.16,
                "profitable_window": True,
                "beat_baseline": True,
            },
        ]
    )
    leaderboard = aggregate_profile_results(frame)
    assert leaderboard.iloc[0]["profile_id"] == "det_orb_oi_combo_v1"
    assert leaderboard.iloc[0]["profitable_window_pct"] == 1.0
    assert leaderboard.iloc[0]["beat_baseline_pct"] == 1.0


def test_build_recommendation_returns_top_profile() -> None:
    leaderboard = pd.DataFrame(
        [
            {
                "profile_id": "det_orb_oi_combo_v1",
                "profile_label": "Combo",
                "windows": 12,
                "total_trades": 64,
                "avg_return_pct": 0.03,
                "median_return_pct": 0.025,
                "profitable_window_pct": 0.75,
                "beat_baseline_pct": 0.66,
                "avg_profit_factor": 1.6,
                "avg_win_rate": 0.58,
                "avg_drawdown_pct": -0.14,
                "worst_window_return_pct": -0.04,
                "worst_drawdown_pct": -0.31,
            }
        ]
    )
    recommendation = build_recommendation(leaderboard)
    assert recommendation["status"] == "ok"
    assert recommendation["recommended_profile_id"] == "det_orb_oi_combo_v1"


def test_memory_signal_logger_accepts_decision_trace_hook() -> None:
    logger = MemorySignalLogger(capital_allocated=500000.0)
    logger.log_decision_trace(
        {
            "trace_id": "trace-1",
            "engine_mode": "deterministic",
            "decision_mode": "rule_vote",
            "final_outcome": "allowed",
        }
    )
