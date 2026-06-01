"""Unit tests for exit policy stack (E2-S1 through E2-S4 DoD)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from strategy_app.contracts import ExitReason, PositionContext
from strategy_app.position.exit_policy import (
    CompositeExitPolicy,
    PremiumTargetPolicy,
    ThesisFailPolicy,
    TrailingStopPolicy,
    build_default_exit_stack,
)


def _pos(**kwargs) -> PositionContext:
    defaults = dict(
        position_id="test-pos",
        direction="PE",
        strike=54200,
        expiry=None,
        entry_premium=1000.0,
        entry_time=datetime.now(),
        entry_snapshot_id="snap",
        lots=1,
        pnl_pct=0.0,
        mfe_pct=0.0,
        mae_pct=0.0,
        bars_held=0,
    )
    defaults.update(kwargs)
    return PositionContext(**defaults)


_snap = MagicMock()


class TestPremiumTargetPolicy:
    def test_fires_when_target_reached(self):
        policy = PremiumTargetPolicy(target_pct=0.015)
        pos = _pos(pnl_pct=0.02)
        assert policy.check(pos, _snap) == ExitReason.TARGET_HIT

    def test_fires_at_exact_target(self):
        policy = PremiumTargetPolicy(target_pct=0.015)
        pos = _pos(pnl_pct=0.015)
        assert policy.check(pos, _snap) == ExitReason.TARGET_HIT

    def test_silent_below_target(self):
        policy = PremiumTargetPolicy(target_pct=0.015)
        pos = _pos(pnl_pct=0.01)
        assert policy.check(pos, _snap) is None

    def test_silent_when_negative(self):
        policy = PremiumTargetPolicy(target_pct=0.015)
        pos = _pos(pnl_pct=-0.05)
        assert policy.check(pos, _snap) is None


class TestTrailingStopPolicy:
    def test_fires_when_trail_breached(self):
        # MFE=1.5%, pnl=0.9%, trail=0.5% → 0.9 < 1.5 - 0.5 = 1.0 → fire
        policy = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)
        pos = _pos(mfe_pct=0.015, pnl_pct=0.009)
        assert policy.check(pos, _snap) == ExitReason.TRAILING_STOP

    def test_silent_below_activation(self):
        # MFE=0.8% < activation=1% → not yet active
        policy = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)
        pos = _pos(mfe_pct=0.008, pnl_pct=0.001)
        assert policy.check(pos, _snap) is None

    def test_silent_within_trail(self):
        # MFE=2%, pnl=1.6%, trail=0.5% → 1.6 > 2.0 - 0.5 = 1.5 → no fire
        policy = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)
        pos = _pos(mfe_pct=0.02, pnl_pct=0.016)
        assert policy.check(pos, _snap) is None

    def test_trail_from_doc_example(self):
        # Trade 2: MFE=1.14%, pnl=-0.64% → 1.14 - 0.5 = 0.64 → pnl < 0.64 → fire
        policy = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)
        pos = _pos(mfe_pct=0.0114, pnl_pct=-0.0064)
        assert policy.check(pos, _snap) == ExitReason.TRAILING_STOP


class TestThesisFailPolicy:
    def test_fires_when_no_mfe_after_bars(self):
        policy = ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002)
        pos = _pos(bars_held=3, mfe_pct=0.0)
        assert policy.check(pos, _snap) == ExitReason.THESIS_FAIL

    def test_silent_below_min_bars(self):
        policy = ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002)
        pos = _pos(bars_held=2, mfe_pct=0.0)
        assert policy.check(pos, _snap) is None

    def test_silent_when_mfe_sufficient(self):
        policy = ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002)
        pos = _pos(bars_held=3, mfe_pct=0.003)
        assert policy.check(pos, _snap) is None

    def test_fires_after_many_bars_with_zero_mfe(self):
        # CE trades 3,4,5 from 2026-06-01: MFE=0%
        policy = ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002)
        pos = _pos(bars_held=5, mfe_pct=0.0, pnl_pct=-0.015)
        assert policy.check(pos, _snap) == ExitReason.THESIS_FAIL


class TestCompositeExitPolicy:
    def test_first_trigger_wins(self):
        p1 = PremiumTargetPolicy(target_pct=0.05)  # won't fire (pnl=0.02)
        p2 = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)  # will fire
        composite = CompositeExitPolicy([p1, p2])
        pos = _pos(mfe_pct=0.015, pnl_pct=0.009)
        assert composite.check(pos, _snap) == ExitReason.TRAILING_STOP

    def test_none_when_no_policy_fires(self):
        composite = CompositeExitPolicy([
            PremiumTargetPolicy(0.05),
            TrailingStopPolicy(0.05, 0.01),
            ThesisFailPolicy(10, 0.002),
        ])
        pos = _pos(pnl_pct=0.01, mfe_pct=0.01, bars_held=1)
        assert composite.check(pos, _snap) is None

    def test_name_is_descriptive(self):
        composite = CompositeExitPolicy([PremiumTargetPolicy(0.015)])
        assert "premium_target" in composite.name


class TestBuildDefaultExitStack:
    def test_builds_without_env(self, monkeypatch):
        monkeypatch.delenv("EXIT_PREMIUM_TARGET_PCT", raising=False)
        monkeypatch.delenv("EXIT_TRAILING_ACTIVATION_PCT", raising=False)
        monkeypatch.delenv("EXIT_TRAILING_TRAIL_PCT", raising=False)
        monkeypatch.delenv("EXIT_THESIS_FAIL_BARS", raising=False)
        monkeypatch.delenv("EXIT_THESIS_FAIL_MIN_MFE", raising=False)
        stack = build_default_exit_stack()
        assert stack is not None
        assert "composite" in stack.name

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("EXIT_PREMIUM_TARGET_PCT", "0.03")
        stack = build_default_exit_stack()
        assert "3.0%" in stack.name
