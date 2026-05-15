"""Unit tests for build_ml_entry_signal — precedence rules between recipe defaults
and explicit caller overrides for risk/hold parameters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytest

from strategy_app.engines.trade_signal_builder import build_ml_entry_signal


@dataclass
class FakeDecision:
    action: str = "BUY_CE"
    reason: str = "staged_entry_ready"
    entry_prob: float = 0.60
    direction_up_prob: float = 0.60
    ce_prob: float = 0.60
    pe_prob: float = 0.40
    recipe_id: str = "L3"
    recipe_prob: float = 0.65
    recipe_margin: float = 0.25
    horizon_minutes: Optional[int] = 20
    stop_loss_pct: Optional[float] = 0.001
    target_pct: Optional[float] = 0.0025
    risk_basis: str = "underlying"


class FakeSnap:
    """Minimal snapshot stand-in matching the duck-typed surface used by builder."""
    def __init__(self) -> None:
        self.snapshot_id = "snap-test-001"
        self.atm_strike = 48000
        self.timestamp_or_now = datetime(2026, 3, 2, 9, 30, tzinfo=timezone.utc)

    def option_ltp(self, direction: str, strike: int) -> Optional[float]:
        return 200.0


class FakeRisk:
    def compute_lots(self, *, entry_premium, stop_loss_pct, confidence) -> int:
        return 5


def _decision() -> FakeDecision:
    return FakeDecision()


def test_recipe_defaults_used_when_no_override():
    """Without overrides, signal should use recipe values from the staged decision."""
    sig = build_ml_entry_signal(
        snap=FakeSnap(),
        decision=_decision(),
        selected_direction="CE",
        selected_strike=48000,
        selected_entry_premium=200.0,
        risk_manager=FakeRisk(),
    )
    assert sig.underlying_stop_pct == pytest.approx(0.001)
    assert sig.underlying_target_pct == pytest.approx(0.0025)
    assert sig.max_hold_bars == 20


def test_phase12_overrides_win_over_recipe():
    """When env-derived overrides are passed, the emitted signal MUST use them
    even though the recipe specifies 0.001 / 0.0025 / 20."""
    sig = build_ml_entry_signal(
        snap=FakeSnap(),
        decision=_decision(),  # recipe: 0.001 / 0.0025 / 20
        selected_direction="CE",
        selected_strike=48000,
        selected_entry_premium=200.0,
        underlying_stop_pct=0.002,
        underlying_target_pct=0.005,
        max_hold_bars_override=30,
        risk_manager=FakeRisk(),
    )
    assert sig.underlying_stop_pct == pytest.approx(0.002)
    assert sig.underlying_target_pct == pytest.approx(0.005)
    assert sig.max_hold_bars == 30


def test_partial_override_stop_only():
    """If only stop is overridden, target and hold keep recipe values."""
    sig = build_ml_entry_signal(
        snap=FakeSnap(),
        decision=_decision(),
        selected_direction="CE",
        selected_strike=48000,
        selected_entry_premium=200.0,
        underlying_stop_pct=0.003,
        risk_manager=FakeRisk(),
    )
    assert sig.underlying_stop_pct == pytest.approx(0.003)
    assert sig.underlying_target_pct == pytest.approx(0.0025)  # recipe
    assert sig.max_hold_bars == 20  # recipe


def test_max_hold_override_zero_intent_explicit():
    """max_hold_bars_override=None means 'use recipe'; passing an int means 'override'."""
    sig = build_ml_entry_signal(
        snap=FakeSnap(),
        decision=_decision(),
        selected_direction="CE",
        selected_strike=48000,
        selected_entry_premium=200.0,
        max_hold_bars_override=45,
        risk_manager=FakeRisk(),
    )
    assert sig.max_hold_bars == 45


def test_no_recipe_no_override_falls_back_to_hardcoded():
    """Pathological case: recipe has no values, no overrides — builder must
    still produce a valid signal (hardcoded last-resort fallback)."""
    dec = _decision()
    dec.stop_loss_pct = 0.0
    dec.target_pct = 0.0
    dec.horizon_minutes = None
    sig = build_ml_entry_signal(
        snap=FakeSnap(),
        decision=dec,
        selected_direction="CE",
        selected_strike=48000,
        selected_entry_premium=200.0,
        risk_manager=FakeRisk(),
    )
    # underlying_stop_pct stays None (no override, no recipe) — tracker treats None as disabled
    assert sig.underlying_stop_pct is None
    assert sig.underlying_target_pct is None
    assert sig.max_hold_bars == 15  # hardcoded fallback in builder
