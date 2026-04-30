from __future__ import annotations

from ml_pipeline_2.factory.selector import select_winner


def test_factory_selector_picks_best_publishable_candidate() -> None:
    winner = select_winner(
        [
            {"lane_id": "a", "profit_factor": 1.4, "net_return_sum": 0.12, "stage2_roc_auc": 0.60},
            {"lane_id": "b", "profit_factor": 1.6, "net_return_sum": 0.10, "stage2_roc_auc": 0.59},
        ],
        strategy="publishable_economics_v1",
    )
    assert winner["lane_id"] == "b"


def test_factory_selector_returns_none_for_empty_candidates() -> None:
    assert select_winner([], strategy="publishable_economics_v1") is None
