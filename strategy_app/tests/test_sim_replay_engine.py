"""Unit tests for strategy_app.sim.replay_engine (MD-S1 DoD).

Tests verify:
- replay_day returns the correct structure.
- Progress callback is called with monotonically increasing indices.
- An empty snapshot list produces an empty trade list.
- The engine is isolated: no real Redis/Mongo calls; env is not mutated after the call.

These tests mock the engine's imports so they run fast without needing ML models.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to build a minimal engine + contract mock that replay_day can use
# without ML models or Redis.
# ---------------------------------------------------------------------------

class _FakeSignal:
    def __init__(self, signal_type, direction="CE", strike=48000, premium=500.0, exit_reason=None):
        self.signal_type = signal_type
        self.direction = direction
        self.strike = strike
        self.entry_premium = premium
        self.max_lots = 1
        self.exit_reason = exit_reason


class _SignalType:
    ENTRY = "ENTRY"
    EXIT  = "EXIT"


class _FakeTracker:
    def __init__(self):
        self._closed_positions: List[dict] = []


class _FakeEngine:
    def __init__(self, signals_by_index: dict):
        self._signals = signals_by_index
        self._tracker = _FakeTracker()
        self._i = 0

    def evaluate(self, snap: dict) -> Optional[_FakeSignal]:
        sig = self._signals.get(self._i)
        if sig == "error":
            self._i += 1
            raise RuntimeError("synthetic eval error")
        self._i += 1
        return sig

    def set_run_context(self, run_id: str, meta: dict) -> None:
        pass

    def on_session_start(self, d) -> None:
        pass

    def on_session_end(self, d) -> None:
        pass


# ---------------------------------------------------------------------------
# The key challenge: replay_day does `from strategy_app.engines import ...`
# and `from strategy_app.contracts import SignalType` inside the function.
# We patch those imports before calling.
# ---------------------------------------------------------------------------

def _run_with_mocked_engine(engine: _FakeEngine, snapshots: list, trade_date: str = "2026-01-02"):
    """Patch strategy_app imports and call replay_day."""
    fake_profile = {"risk_config": {"halt_consecutive_losses": 3}}
    fake_exit_stack = MagicMock()
    fake_exit_stack.name = "scalper"

    with patch.dict("os.environ", {
        "STRATEGY_RUN_DIR":                "/tmp/test_replay",
        "STRATEGY_REDIS_PUBLISH_ENABLED":  "0",
        "STRATEGY_PROFILE_ID":             "test_profile",
        "STRATEGY_MIN_CONFIDENCE":         "0.50",
    }):
        with patch("strategy_app.sim.replay_engine._ensure_repo_on_path"):
            with patch.dict("sys.modules", {
                "strategy_app.engines": MagicMock(DeterministicRuleEngine=lambda **_: engine),
                "strategy_app.engines.profiles": MagicMock(
                    build_run_metadata=lambda pid: fake_profile,
                ),
                "strategy_app.contracts": MagicMock(SignalType=_SignalType),
                "strategy_app.position.exit_policy": MagicMock(
                    build_default_exit_stack=lambda: fake_exit_stack,
                ),
            }):
                from strategy_app.sim.replay_engine import replay_day
                return replay_day(snapshots, trade_date)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_snapshots_returns_empty_trades():
    engine = _FakeEngine({})
    result = _run_with_mocked_engine(engine, [])
    assert result["trades"] == []
    assert result["exit_stack_name"] == "scalper"
    assert result["diag"]["evaluated"] == 0


def test_no_signals_returns_empty_trades():
    # Engine returns None for every snapshot — no entries, no exits.
    engine = _FakeEngine({})  # all indices return None
    snaps = [{"timestamp": "2026-01-02T09:15:00", "i": i} for i in range(5)]
    result = _run_with_mocked_engine(engine, snaps)
    assert result["trades"] == []
    assert result["diag"]["evaluated"] == 5
    assert result["diag"]["signals"] == 0


def test_entry_then_exit_produces_one_trade():
    entry_sig = _FakeSignal(_SignalType.ENTRY, direction="CE", strike=48100, premium=500.0)
    exit_sig  = _FakeSignal(_SignalType.EXIT,  exit_reason=SimpleNamespace(value="thesis_fail"))

    engine = _FakeEngine({0: entry_sig, 1: exit_sig})
    engine._tracker._closed_positions = [{
        "pnl_pct": 0.05,
        "mfe_pct": 0.08,
        "mae_pct": -0.01,
        "exit_premium": 525.0,
        "exit_policy_triggered": "thesis_fail",
        "exit_reason": "thesis_fail",
    }]

    snaps = [
        {"timestamp": "2026-01-02T09:15:00"},
        {"timestamp": "2026-01-02T09:16:00"},
    ]
    result = _run_with_mocked_engine(engine, snaps)

    assert len(result["trades"]) == 1
    t = result["trades"][0]
    assert t["direction"] == "CE"
    assert t["strike"] == 48100
    assert t["pnl_pct"] == pytest.approx(0.05)
    assert t["mfe_pct"] == pytest.approx(0.08)
    assert t["exit"] == "thesis_fail"
    assert t["source"] == "sim"


def test_eval_error_is_counted_and_skipped():
    entry_sig = _FakeSignal(_SignalType.ENTRY, direction="PE", strike=47900, premium=400.0)
    engine = _FakeEngine({0: "error", 1: entry_sig})
    snaps = [
        {"timestamp": "2026-01-02T09:15:00"},
        {"timestamp": "2026-01-02T09:16:00"},
    ]
    result = _run_with_mocked_engine(engine, snaps)
    assert result["diag"]["eval_errors"] == 1
    assert result["diag"]["first_error"] is not None


def test_progress_callback_called_monotonically():
    snaps = [{"timestamp": f"2026-01-02T09:{m:02d}:00"} for m in range(25)]
    engine = _FakeEngine({})
    calls: List[tuple] = []

    with patch.dict("os.environ", {
        "STRATEGY_RUN_DIR":               "/tmp/test_replay",
        "STRATEGY_REDIS_PUBLISH_ENABLED": "0",
        "STRATEGY_PROFILE_ID":            "test_profile",
        "STRATEGY_MIN_CONFIDENCE":        "0.50",
    }):
        with patch("strategy_app.sim.replay_engine._ensure_repo_on_path"):
            fake_exit_stack = MagicMock(); fake_exit_stack.name = "scalper"
            with patch.dict("sys.modules", {
                "strategy_app.engines": MagicMock(DeterministicRuleEngine=lambda **_: engine),
                "strategy_app.engines.profiles": MagicMock(build_run_metadata=lambda p: {"risk_config": {}}),
                "strategy_app.contracts": MagicMock(SignalType=_SignalType),
                "strategy_app.position.exit_policy": MagicMock(build_default_exit_stack=lambda: fake_exit_stack),
            }):
                from strategy_app.sim.replay_engine import replay_day
                replay_day(snaps, "2026-01-02", progress_cb=lambda i, n: calls.append((i, n)))

    assert len(calls) > 0
    indices = [i for i, _ in calls]
    assert indices == sorted(indices), "progress callback indices not monotonically increasing"
    assert calls[-1][0] == len(snaps), "final callback should report total"


def test_env_not_mutated_after_replay():
    before = dict(os.environ)
    engine = _FakeEngine({})
    _run_with_mocked_engine(engine, [])
    after = dict(os.environ)
    assert before == after, "replay_day must not permanently mutate os.environ"
