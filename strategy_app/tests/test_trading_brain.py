"""Tests for TradingBrain and its components."""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from strategy_app.brain.brain import BrainDecision, TradingBrain
from strategy_app.brain.consensus import ConsensusGate, ConsensusResult
from strategy_app.brain.context import DayContext, DayScore, FitnessScore, SessionCarry
from strategy_app.brain.fitness import StrategyFitnessEvaluator
from strategy_app.brain.plugin import ContextProvider, StrategyPlugin
from strategy_app.brain.providers.daily_features import DailyFeaturesProvider
from strategy_app.brain.providers.llm_stub import LLMContextProvider
from strategy_app.brain.session_memory import SessionMemory
from strategy_app.contracts import Direction, SignalType, StrategyVote


# ─────────────────────────── Helpers ────────────────────────────────────────

def _make_vote(direction: Direction, strategy: str = "STRAT_A", confidence: float = 0.8) -> StrategyVote:
    return StrategyVote(
        strategy_name=strategy,
        snapshot_id="snap_001",
        timestamp=__import__("datetime").datetime(2024, 5, 15, 10, 0),
        trade_date="2024-05-15",
        signal_type=SignalType.ENTRY,
        direction=direction,
        confidence=confidence,
        reason="test",
        raw_signals={},
    )


def _calm_context(trade_date: date = date(2024, 5, 15)) -> DayContext:
    return DayContext(
        trade_date=trade_date,
        day_score=DayScore.CALM,
        day_score_confidence=0.85,
        day_score_reason="test_calm",
    )


def _avoid_context(trade_date: date = date(2024, 5, 15)) -> DayContext:
    return DayContext(
        trade_date=trade_date,
        day_score=DayScore.AVOID,
        day_score_confidence=0.99,
        day_score_reason="test_avoid",
    )


# ─────────────────────────── DayScore / DayContext ──────────────────────────

class TestDayContext:
    def test_to_dict_roundtrip(self):
        ctx = _calm_context()
        d = ctx.to_dict()
        assert d["day_score"] == "CALM"
        assert d["trade_date"] == "2024-05-15"

    def test_session_carry_defaults(self):
        carry = SessionCarry.empty()
        assert carry.consecutive_losses_at_close == 0
        assert carry.losing_streak_days == 0

    def test_session_carry_roundtrip(self):
        carry = SessionCarry(
            consecutive_losses_at_close=2,
            prior_day_pnl_pct=-0.03,
            losing_streak_days=1,
            last_trade_date=date(2024, 5, 14),
        )
        raw = carry.to_dict()
        restored = SessionCarry.from_dict(raw)
        assert restored.consecutive_losses_at_close == 2
        assert restored.losing_streak_days == 1
        assert restored.last_trade_date == date(2024, 5, 14)


# ─────────────────────────── ConsensusGate ──────────────────────────────────

class TestConsensusGate:
    def test_disabled_always_allows(self):
        gate = ConsensusGate(min_agreeing=0)
        result = gate.evaluate([_make_vote(Direction.CE)])
        assert result.allowed
        assert result.reason == "consensus_disabled"

    def test_single_vote_min1_passes(self):
        gate = ConsensusGate(min_agreeing=1)
        result = gate.evaluate([_make_vote(Direction.CE)])
        assert result.allowed
        assert result.agreed_direction == "CE"

    def test_no_votes_blocks(self):
        gate = ConsensusGate(min_agreeing=1)
        result = gate.evaluate([])
        assert not result.allowed
        assert result.reason == "no_entry_votes"

    def test_min2_single_vote_blocks(self):
        gate = ConsensusGate(min_agreeing=2)
        result = gate.evaluate([_make_vote(Direction.CE)])
        assert not result.allowed
        assert "consensus_not_met" in result.reason

    def test_min2_two_ce_votes_passes(self):
        gate = ConsensusGate(min_agreeing=2)
        votes = [_make_vote(Direction.CE, "A"), _make_vote(Direction.CE, "B")]
        result = gate.evaluate(votes)
        assert result.allowed
        assert result.agreed_direction == "CE"
        assert result.agreeing_count == 2

    def test_direction_conflict_majority_wins(self):
        gate = ConsensusGate(min_agreeing=2)
        votes = [
            _make_vote(Direction.CE, "A"),
            _make_vote(Direction.CE, "B"),
            _make_vote(Direction.PE, "C"),
        ]
        result = gate.evaluate(votes)
        assert result.allowed
        assert result.agreed_direction == "CE"
        assert "with_minority_conflict" in result.reason

    def test_require_direction_blocks_conflict(self):
        gate = ConsensusGate(min_agreeing=1, require_direction_agreement=True)
        votes = [_make_vote(Direction.CE, "A"), _make_vote(Direction.PE, "B")]
        result = gate.evaluate(votes)
        assert not result.allowed
        assert "direction_conflict_blocked" in result.reason


# ─────────────────────────── StrategyFitnessEvaluator ───────────────────────

class TestStrategyFitnessEvaluator:
    def test_avoid_blocks_everything(self):
        ctx = _avoid_context()
        ev = StrategyFitnessEvaluator()
        score = ev.evaluate("R1S_TOP3_SHORT_CE", ctx)
        assert not score.fits
        assert score.size_multiplier == 0.0

    def test_calm_favours_short_premium(self):
        ctx = _calm_context()
        ev = StrategyFitnessEvaluator()
        score = ev.evaluate("R1S_TOP3_SHORT_CE", ctx)
        assert score.fits
        assert score.size_multiplier == 1.0
        assert score.score >= 0.9

    def test_volatile_blocks_short_premium(self):
        ctx = DayContext(trade_date=date(2024, 5, 15), day_score=DayScore.VOLATILE)
        ev = StrategyFitnessEvaluator()
        score = ev.evaluate("PBV1_TOP3_THESIS", ctx)
        assert not score.fits
        assert score.size_multiplier == 0.0

    def test_volatile_reduces_other_strategies(self):
        ctx = DayContext(trade_date=date(2024, 5, 15), day_score=DayScore.VOLATILE)
        ev = StrategyFitnessEvaluator()
        score = ev.evaluate("ORB", ctx)
        assert score.fits
        assert score.size_multiplier == 0.5

    def test_plugin_overrides_default(self):
        class AlwaysFit(StrategyPlugin):
            name = "ORB"
            def fits(self, context):
                return FitnessScore(strategy_name="ORB", fits=True, score=1.0, size_multiplier=0.99)

        ctx = _avoid_context()
        ev = StrategyFitnessEvaluator()
        ev.register(AlwaysFit())
        score = ev.evaluate("ORB", ctx)
        assert score.fits
        assert score.size_multiplier == pytest.approx(0.99)

    def test_active_strategies_filters(self):
        ctx = _avoid_context()
        ev = StrategyFitnessEvaluator()
        active = ev.active_strategies(ctx, ["R1S_TOP3_SHORT_CE", "ORB"])
        assert active == []

    def test_calm_active_strategies(self):
        ctx = _calm_context()
        ev = StrategyFitnessEvaluator()
        active = ev.active_strategies(ctx, ["R1S_TOP3_SHORT_CE", "ORB"])
        assert "R1S_TOP3_SHORT_CE" in active
        assert "ORB" in active


# ─────────────────────────── SessionMemory ──────────────────────────────────

class TestSessionMemory:
    def test_empty_carry_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = SessionMemory(memory_dir=Path(tmpdir))
            carry = mem.load_carry(date(2024, 5, 15))
            assert carry.consecutive_losses_at_close == 0
            assert carry.last_trade_date is None

    def test_save_and_load_carry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = SessionMemory(memory_dir=Path(tmpdir))
            mem.save_summary(
                trade_date=date(2024, 5, 14),
                trades=4,
                wins=1,
                losses=3,
                consecutive_losses=3,
                session_pnl_pct=-0.025,
            )
            carry = mem.load_carry(date(2024, 5, 15))
            assert carry.consecutive_losses_at_close == 3
            assert carry.prior_day_pnl_pct == pytest.approx(-0.025)
            assert carry.last_trade_date == date(2024, 5, 14)

    def test_carry_does_not_load_future_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = SessionMemory(memory_dir=Path(tmpdir))
            mem.save_summary(
                trade_date=date(2024, 5, 16),
                trades=3,
                wins=3,
                losses=0,
                consecutive_losses=0,
                session_pnl_pct=0.04,
            )
            # Loading for a date before the saved record → empty carry
            carry = mem.load_carry(date(2024, 5, 15))
            assert carry.consecutive_losses_at_close == 0

    def test_most_recent_prior_record_wins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = SessionMemory(memory_dir=Path(tmpdir))
            mem.save_summary(
                trade_date=date(2024, 5, 13),
                trades=2, wins=2, losses=0, consecutive_losses=0, session_pnl_pct=0.01,
            )
            mem.save_summary(
                trade_date=date(2024, 5, 14),
                trades=4, wins=1, losses=3, consecutive_losses=3, session_pnl_pct=-0.03,
            )
            carry = mem.load_carry(date(2024, 5, 15))
            assert carry.consecutive_losses_at_close == 3


# ─────────────────────────── DailyFeaturesProvider ──────────────────────────

class TestDailyFeaturesProvider:
    def _write_features(self, tmpdir: str, data: dict) -> Path:
        path = Path(tmpdir) / "daily_regime_features.json"
        path.write_text(json.dumps(data))
        return path

    def test_returns_empty_when_file_missing(self):
        provider = DailyFeaturesProvider(path=Path("/no/such/file.json"))
        result = provider.provide(date(2024, 5, 15))
        assert result == {}

    def test_loads_date_keyed_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_features(tmpdir, {
                "2024-05-15": {
                    "regime_rv20": 0.0085,
                    "regime_dist_sma20": 0.012,
                    "regime_sma20_slope": 0.0003,
                    "regime_60d_return": 0.07,
                }
            })
            provider = DailyFeaturesProvider(path=path)
            result = provider.provide(date(2024, 5, 15))
            assert result["daily.regime_rv20"] == pytest.approx(0.0085)
            assert result["daily.day_score_hint"] == "CALM"

    def test_volatile_hint_when_high_rv20(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_features(tmpdir, {
                "2024-05-15": {"regime_rv20": 0.025}
            })
            provider = DailyFeaturesProvider(path=path)
            result = provider.provide(date(2024, 5, 15))
            assert result["daily.day_score_hint"] == "VOLATILE"

    def test_list_format_loaded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_features(tmpdir, [
                {"date": "2024-05-15", "regime_rv20": 0.0090, "regime_sma20_slope": 0.0005}
            ])
            provider = DailyFeaturesProvider(path=path)
            result = provider.provide(date(2024, 5, 15))
            assert "daily.regime_rv20" in result

    def test_missing_date_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_features(tmpdir, {"2024-05-14": {"regime_rv20": 0.009}})
            provider = DailyFeaturesProvider(path=path)
            result = provider.provide(date(2024, 5, 15))
            assert result == {}


# ─────────────────────────── LLMContextProvider ─────────────────────────────

class TestLLMContextProvider:
    def test_returns_empty_when_disabled(self):
        provider = LLMContextProvider()
        # BRAIN_LLM_ENABLED is not set in test env → disabled
        result = provider.provide(date(2024, 5, 15))
        assert result == {}


# ─────────────────────────── TradingBrain ───────────────────────────────────

class TestTradingBrain:
    def _calm_brain(self) -> TradingBrain:
        """Brain with a stubbed DailyFeaturesProvider that always returns CALM."""
        class CalmProvider(ContextProvider):
            name = "calm_stub"
            def provide(self, trade_date):
                return {
                    "daily.regime_rv20": 0.007,
                    "daily.regime_sma20_slope": 0.0004,
                    "daily.day_score_hint": "CALM",
                }

        return TradingBrain(
            context_providers=[CalmProvider()],
            consensus_gate=ConsensusGate(min_agreeing=1),
            enabled=True,
        )

    def _avoid_brain(self) -> TradingBrain:
        class AvoidProvider(ContextProvider):
            name = "avoid_stub"
            def provide(self, trade_date):
                return {"daily.day_score_hint": "AVOID"}

        return TradingBrain(
            context_providers=[AvoidProvider()],
            consensus_gate=ConsensusGate(min_agreeing=1),
            enabled=True,
        )

    def test_morning_briefing_returns_context(self):
        brain = self._calm_brain()
        ctx = brain.morning_briefing(date(2024, 5, 15))
        assert ctx.day_score == DayScore.CALM
        assert ctx.day_score_confidence > 0

    def test_gate_entry_allows_on_calm(self):
        brain = self._calm_brain()
        brain.morning_briefing(date(2024, 5, 15))
        votes = [_make_vote(Direction.CE)]
        decision = brain.gate_entry(votes)
        assert decision.allowed
        assert decision.day_score == "CALM"

    def test_gate_entry_blocks_on_avoid(self):
        brain = self._avoid_brain()
        brain.morning_briefing(date(2024, 5, 15))
        votes = [_make_vote(Direction.CE)]
        decision = brain.gate_entry(votes)
        assert not decision.allowed
        assert decision.reason == "day_score_avoid"

    def test_gate_entry_blocks_on_consensus_failure(self):
        brain = TradingBrain(
            context_providers=[],
            consensus_gate=ConsensusGate(min_agreeing=2),
            enabled=True,
        )
        brain.morning_briefing(date(2024, 5, 15))
        votes = [_make_vote(Direction.CE, "A")]  # only 1, need 2
        decision = brain.gate_entry(votes)
        assert not decision.allowed
        assert "consensus_gate" in decision.reason

    def test_gate_entry_passthrough_when_disabled(self):
        brain = TradingBrain(enabled=False)
        brain.morning_briefing(date(2024, 5, 15))
        votes = []  # even empty votes pass when disabled
        decision = brain.gate_entry(votes)
        assert decision.allowed
        assert decision.reason == "brain_disabled"

    def test_trade_result_updates_counters(self):
        brain = self._calm_brain()
        brain.morning_briefing(date(2024, 5, 15))
        brain.on_trade_result(pnl_pct=0.05)
        brain.on_trade_result(pnl_pct=-0.02)
        brain.on_trade_result(pnl_pct=-0.03)
        summary = brain.context_summary()
        assert summary["session"]["trades"] == 3
        assert summary["session"]["wins"] == 1
        assert summary["session"]["losses"] == 2
        assert summary["session"]["consecutive_losses"] == 2

    def test_save_and_load_session_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = SessionMemory(memory_dir=Path(tmpdir))
            brain = TradingBrain(
                context_providers=[],
                session_memory=mem,
                enabled=True,
            )
            brain.morning_briefing(date(2024, 5, 14))
            brain.on_trade_result(pnl_pct=-0.01)
            brain.on_trade_result(pnl_pct=-0.02)
            brain.on_trade_result(pnl_pct=-0.03)
            brain.save_session_summary(date(2024, 5, 14))

            # Next day: consecutive losses should carry over
            carry = mem.load_carry(date(2024, 5, 15))
            assert carry.consecutive_losses_at_close == 3

    def test_losing_streak_days_triggers_avoid(self):
        """Three consecutive losing sessions → next day AVOID."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = SessionMemory(memory_dir=Path(tmpdir))
            for d, losses in [
                (date(2024, 5, 12), 2),
                (date(2024, 5, 13), 3),
                (date(2024, 5, 14), 3),
            ]:
                mem.save_summary(
                    trade_date=d, trades=3, wins=0, losses=3,
                    consecutive_losses=losses, session_pnl_pct=-0.02,
                )
            brain = TradingBrain(
                context_providers=[],
                session_memory=mem,
                enabled=True,
            )
            ctx = brain.morning_briefing(date(2024, 5, 15))
            assert ctx.day_score == DayScore.AVOID
