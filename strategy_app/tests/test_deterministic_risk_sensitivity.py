from __future__ import annotations

import pandas as pd

from strategy_app.tools.deterministic_risk_sensitivity import (
    aggregate_variant_results,
    build_recommendation,
    default_winner_variants,
)


def test_default_winner_variants_cover_stop_and_trailing_matrix() -> None:
    variants = default_winner_variants()
    ids = [item.profile_id for item in variants]
    assert len(variants) == 8
    assert len(ids) == len(set(ids))
    assert "det_orb_oi_safe_sl20_trail" in ids
    assert "det_orb_oi_safe_sl20_hard" in ids


def test_variant_recommendation_prefers_stability() -> None:
    frame = pd.DataFrame(
        [
            {
                "window_label": "2024-Q1",
                "profile_id": "det_orb_oi_safe_sl20_trail",
                "profile_label": "SL 20 + Trail",
                "trades": 10,
                "net_capital_return_pct": 0.01,
                "profit_factor": 1.3,
                "win_rate": 0.6,
                "max_drawdown_pct": -0.02,
                "stop_loss_exit_pct": 0.2,
                "trailing_stop_exit_pct": 0.5,
                "profitable_window": True,
                "beat_baseline": False,
            },
            {
                "window_label": "2024-Q2",
                "profile_id": "det_orb_oi_safe_sl20_trail",
                "profile_label": "SL 20 + Trail",
                "trades": 9,
                "net_capital_return_pct": 0.015,
                "profit_factor": 1.4,
                "win_rate": 0.6,
                "max_drawdown_pct": -0.01,
                "stop_loss_exit_pct": 0.1,
                "trailing_stop_exit_pct": 0.6,
                "profitable_window": True,
                "beat_baseline": False,
            },
            {
                "window_label": "2024-Q1",
                "profile_id": "det_orb_oi_safe_sl25_hard",
                "profile_label": "SL 25 Only",
                "trades": 11,
                "net_capital_return_pct": 0.02,
                "profit_factor": 1.2,
                "win_rate": 0.5,
                "max_drawdown_pct": -0.03,
                "stop_loss_exit_pct": 0.4,
                "trailing_stop_exit_pct": 0.0,
                "profitable_window": True,
                "beat_baseline": True,
            },
            {
                "window_label": "2024-Q2",
                "profile_id": "det_orb_oi_safe_sl25_hard",
                "profile_label": "SL 25 Only",
                "trades": 11,
                "net_capital_return_pct": -0.01,
                "profit_factor": 0.9,
                "win_rate": 0.4,
                "max_drawdown_pct": -0.05,
                "stop_loss_exit_pct": 0.5,
                "trailing_stop_exit_pct": 0.0,
                "profitable_window": False,
                "beat_baseline": False,
            },
        ]
    )
    leaderboard = aggregate_variant_results(frame)
    assert leaderboard.iloc[0]["profile_id"] == "det_orb_oi_safe_sl20_trail"
    recommendation = build_recommendation(leaderboard)
    assert recommendation["status"] == "ok"
    assert recommendation["recommended_variant_id"] == "det_orb_oi_safe_sl20_trail"
