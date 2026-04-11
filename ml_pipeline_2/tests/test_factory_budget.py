from __future__ import annotations

from ml_pipeline_2.factory.budget import ResourceBudget


def test_factory_budget_blocks_until_released() -> None:
    budget = ResourceBudget(total_cores=8, total_memory_gb=32)
    assert budget.can_afford(4, 8)
    budget.acquire("lane1", 4, 8)
    assert not budget.can_afford(5, 8)
    budget.release("lane1")
    assert budget.can_afford(5, 8)


def test_factory_budget_release_is_idempotent() -> None:
    budget = ResourceBudget(total_cores=8, total_memory_gb=32)
    budget.acquire("lane1", 4, 8)
    budget.release("lane1")
    budget.release("lane1")
    assert budget.available()[0] == 8


def test_factory_budget_applies_memory_headroom() -> None:
    budget = ResourceBudget(total_cores=8, total_memory_gb=256)
    assert round(budget.effective_memory_gb, 1) == 217.6
