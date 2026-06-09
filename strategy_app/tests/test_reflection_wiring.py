"""Wiring test: the engine journals closed trades without breaking the close path.

Exercises ``DeterministicRuleEngine._journal_closed_trade`` in isolation (the
method only touches the position, the exit signal, and its own cost helper), so
no heavy engine init is needed — ``object.__new__`` bypasses ``__init__``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

from strategy_app.contracts import ExitReason, PositionContext
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine


def _engine() -> DeterministicRuleEngine:
    # bypass __init__ — _journal_closed_trade is self-contained
    return object.__new__(DeterministicRuleEngine)


def _position(**kw) -> PositionContext:
    base = dict(
        position_id="p1", direction="CE", strike=54000, expiry=None,
        entry_premium=200.0, entry_time=datetime(2026, 6, 7, 10, 0, 0),
        entry_snapshot_id="s1", lots=1, current_premium=199.0,
        pnl_pct=-0.008, mfe_pct=0.05, mae_pct=-0.02,
        target_pct=0.40, stop_loss_pct=0.20, bars_held=5,
        decision_metrics={},
    )
    base.update(kw)
    return PositionContext(**base)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BRAIN_REFLECTION_ENABLED", raising=False)
    monkeypatch.delenv("STRATEGY_LOT_SIZE", raising=False)
    yield


class TestJournalWiring:
    def test_attaches_numeric_flags_and_logs_record(self, caplog):
        eng, pos = _engine(), _position()
        sig = SimpleNamespace(exit_reason=ExitReason.STOP_LOSS)
        with caplog.at_level(logging.INFO):
            eng._journal_closed_trade(sig, pos)

        # numeric flags rode into decision_metrics (durable POSITION_CLOSE record)
        assert pos.decision_metrics["reflection_is_loss"] == 1.0
        assert "reflection_needs_reasoning" in pos.decision_metrics
        assert "reflection_overpaid" in pos.decision_metrics

        # full record (with the string tag) went to the trade_journal log line
        line = next((r.getMessage() for r in caplog.records
                     if str(r.msg).startswith("trade_journal")), None)
        assert line is not None
        rec = json.loads(line.split(" ", 1)[1])
        # gross +0.5% flipped negative by costs => cost_miss
        assert rec["autopsy"]["tag"] == "cost_miss"
        assert rec["position_id"] == "p1"

    def test_disabled_flag_is_a_noop(self, monkeypatch):
        monkeypatch.setenv("BRAIN_REFLECTION_ENABLED", "false")
        eng, pos = _engine(), _position()
        eng._journal_closed_trade(SimpleNamespace(exit_reason=ExitReason.STOP_LOSS), pos)
        assert "reflection_is_loss" not in pos.decision_metrics

    def test_never_raises_on_bad_input(self, caplog):
        eng = _engine()
        # a position-like object that explodes when read => must be swallowed
        class Boom:
            position_id = "boom"
            @property
            def decision_metrics(self):
                raise RuntimeError("kaboom")
        with caplog.at_level(logging.WARNING):
            eng._journal_closed_trade(SimpleNamespace(exit_reason=None), Boom())
        # no exception propagated; a warning was logged
        assert any("trade_journal failed" in r.message for r in caplog.records)

    def test_cost_frac_falls_back_when_no_notional(self):
        eng = _engine()
        assert eng._closed_trade_cost_frac(_position(entry_premium=0.0)) == 0.013

    def test_cost_frac_uses_cost_model(self):
        eng = _engine()
        frac = eng._closed_trade_cost_frac(_position(entry_premium=200.0, current_premium=199.0, lots=1))
        # 1-lot @200 prem, lot 30 => ~0.85% round trip, well under 1.3% fallback
        assert 0.0 < frac < 0.013
