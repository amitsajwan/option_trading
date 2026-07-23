"""_await_fill (fill-truth reconciliation, tracker.py/_build_entry_signal) must
never arm for sim/replay run_ids (found 2026-07-23).

execution_app is never in the loop during a sim/replay run -- its signals are
hard-blocked at the broker boundary by the same run_id prefix check -- so no
fill-confirmation event can ever arrive. Before this fix, EVERY tier=="live"
signal armed the wait regardless of run_id, so every live-tier replay position
was silently cancelled via cancel_position("fill_confirm_timeout") after
exactly FILL_CONFIRM_BARS (default 5) bars: no exit signal, no positions.jsonl
close record, has_position flipping True->False with nothing in between.
Confirmed live via a 2026-07-23 OOS replay (5 phantom "trades" opening every
~6 bars on one day, all tier=live, zero EXIT signals in signals.jsonl).
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from strategy_app.contracts import Direction, SignalType, StrategyVote
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.market.regime import Regime, RegimeSignal
from strategy_app.market.snapshot_accessor import SnapshotAccessor


def _snapshot(*, snapshot_id: str, ts: str, ce_price: float = 100.0, pe_price: float = 100.0,
              atm_strike: int = 50000) -> SnapshotAccessor:
    payload: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "session_context": {
            "snapshot_id": snapshot_id, "timestamp": ts, "date": ts[:10],
            "session_phase": "ACTIVE", "days_to_expiry": 1,
        },
        "chain_aggregates": {"atm_strike": atm_strike, "strike_count": 1},
        "atm_options": {"atm_ce_close": ce_price, "atm_pe_close": pe_price},
        "strikes": [{"strike": float(atm_strike), "ce_ltp": ce_price, "pe_ltp": pe_price}],
    }
    return SnapshotAccessor(payload)


def _live_tier_vote(snapshot_id: str, ts: datetime) -> StrategyVote:
    return StrategyVote(
        strategy_name="TEST", snapshot_id=snapshot_id, timestamp=ts,
        trade_date=ts.date().isoformat(), signal_type=SignalType.ENTRY,
        direction=Direction.CE, confidence=0.80, reason="test vote",
        proposed_strike=50000, proposed_entry_premium=100.0,
        raw_signals={"tier": "live", "entry_grade": "GOOD"},
    )


def _build_and_open(engine: DeterministicRuleEngine, run_id: str) -> None:
    engine.on_session_start(date(2026, 2, 28))
    engine.set_run_context(run_id, {})
    ts = datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc)
    vote = _live_tier_vote("snap-1", ts)
    snap = _snapshot(snapshot_id="snap-1", ts="2026-02-28T09:30:00+00:00")
    regime_signal = RegimeSignal(regime=Regime.TRENDING, confidence=0.90, reason="test", evidence={})
    signal = engine._build_entry_signal(vote, snap, engine._risk.context, [vote], regime_signal)
    assert signal is not None, "test setup produced no entry signal -- fixture is broken, not the code under test"
    assert signal.raw_signals.get("tier") == "live"


def _with_redis_publish_disabled(fn):
    prior = os.environ.get("STRATEGY_REDIS_PUBLISH_ENABLED")
    os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = "0"
    try:
        fn()
    finally:
        if prior is None:
            os.environ.pop("STRATEGY_REDIS_PUBLISH_ENABLED", None)
        else:
            os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = prior


def test_await_fill_not_armed_for_replay_run_id():
    def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            from strategy_app.logging.signal_logger import SignalLogger
            engine = DeterministicRuleEngine(signal_logger=SignalLogger(Path(tmpdir)))
            _build_and_open(engine, "replay-multiday-2026-07-15-banknifty")
            assert engine._await_fill is None
    _with_redis_publish_disabled(_run)


def test_await_fill_not_armed_for_sim_run_id():
    def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            from strategy_app.logging.signal_logger import SignalLogger
            engine = DeterministicRuleEngine(signal_logger=SignalLogger(Path(tmpdir)))
            _build_and_open(engine, "sim-2026-07-15-banknifty")
            assert engine._await_fill is None
    _with_redis_publish_disabled(_run)


def test_await_fill_still_armed_for_a_real_live_run_id():
    """Regression guard: the sim/replay exclusion must not accidentally
    swallow genuine live runs too -- fill-truth reconciliation still needs
    to run for the real book."""
    def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            from strategy_app.logging.signal_logger import SignalLogger
            engine = DeterministicRuleEngine(signal_logger=SignalLogger(Path(tmpdir)))
            _build_and_open(engine, "live-banknifty-run-1")
            assert engine._await_fill is not None
            assert engine._await_fill["confirmed"] is False
            assert engine._await_fill["bars"] == 0
    _with_redis_publish_disabled(_run)


def test_await_fill_still_armed_when_run_id_is_none():
    """Live production doesn't always set an explicit run_id; must fail
    open toward the safer (armed) behavior, not silently skip reconciliation."""
    def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            from strategy_app.logging.signal_logger import SignalLogger
            engine = DeterministicRuleEngine(signal_logger=SignalLogger(tmpdir and Path(tmpdir)))
            engine.on_session_start(date(2026, 2, 28))
            # No set_run_context call -- engine._run_id stays None/default.
            ts = datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc)
            vote = _live_tier_vote("snap-1", ts)
            snap = _snapshot(snapshot_id="snap-1", ts="2026-02-28T09:30:00+00:00")
            regime_signal = RegimeSignal(regime=Regime.TRENDING, confidence=0.90, reason="test", evidence={})
            signal = engine._build_entry_signal(vote, snap, engine._risk.context, [vote], regime_signal)
            assert signal is not None
            assert engine._await_fill is not None
    _with_redis_publish_disabled(_run)
