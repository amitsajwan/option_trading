"""Unit tests for the 6 entry pipeline gates and evaluate_v2().

All tests are pure (no engine boot, no Redis, no Mongo).
Each gate is instantiated directly; ctx is built via helpers.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from strategy_app.contracts import Direction, RiskContext, SignalType, StrategyVote
from strategy_app.engines.entry_config import EntryConfig
from strategy_app.engines.entry_pipeline_contracts import (
    EntryContext,
    GateOutcome,
    run_chain,
)
from strategy_app.engines.entry_pipeline_gates import (
    ConfidenceGate,
    DirectionGate,
    EntryPolicyGate,
    HardGatesGate,
    RegimeConfidenceGate,
    StrikeDepthGate,
    VotesGate,
    evaluate_v2,
)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> EntryConfig:
    env: dict[str, str] = {
        "STRATEGY_MIN_CONFIDENCE": "0.65",
        "CONSENSUS_BYPASS_MIN_CONFIDENCE": "0.65",
        "STRATEGY_REGIME_MIN_CONFIDENCE": "0.60",
        "SMART_STRIKE_MAX_PREMIUM": "0",
        "SMART_STRIKE_HARD_PREMIUM_CAP": "1",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return EntryConfig.from_env(env)


def _snap(
    *,
    is_valid_entry_phase: bool = True,
    timestamp=None,
    atm_strike: int = 50000,
    atm_ce_close: float = 100.0,
    atm_pe_close: float = 100.0,
) -> MagicMock:
    snap = MagicMock()
    snap.is_valid_entry_phase = is_valid_entry_phase
    snap.timestamp = timestamp or datetime(2024, 8, 1, 9, 35)
    snap.atm_strike = atm_strike
    snap.atm_ce_close = atm_ce_close
    snap.atm_pe_close = atm_pe_close
    snap.option_ltp = MagicMock(return_value=atm_ce_close)
    return snap


def _regime(*, confidence: float = 0.80, name: str = "TRENDING") -> MagicMock:
    r = MagicMock()
    r.confidence = confidence
    r.regime = MagicMock()
    r.regime.value = name
    return r


def _risk(*, daily_loss_breached: bool = False) -> MagicMock:
    rk = MagicMock(spec=RiskContext)
    rk.daily_loss_breached = daily_loss_breached
    return rk


def _vote(
    *,
    name: str = "ML_ENTRY",
    direction: Direction = Direction.CE,
    confidence: float = 0.80,
    signal_type: SignalType = SignalType.ENTRY,
) -> StrategyVote:
    return StrategyVote(
        strategy_name=name,
        snapshot_id="snap1",
        timestamp=datetime(2024, 8, 1, 9, 35),
        trade_date="2024-08-01",
        signal_type=signal_type,
        direction=direction,
        confidence=confidence,
        reason="test",
        raw_signals={},
    )


def _ctx(
    *,
    votes=None,
    snap=None,
    regime=None,
    risk=None,
    cfg=None,
) -> EntryContext:
    return EntryContext(
        snap=snap or _snap(),
        regime=regime or _regime(),
        risk=risk or _risk(),
        votes=votes or [],
        config=cfg or _cfg(),
    )


# ===========================================================================
# Gate 1 — HardGatesGate
# ===========================================================================

class TestHardGatesGate:
    def test_pass_when_valid(self):
        ctx = _ctx()
        r = HardGatesGate().apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_veto_invalid_entry_phase(self):
        ctx = _ctx(snap=_snap(is_valid_entry_phase=False))
        r = HardGatesGate().apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "invalid_entry_phase"

    def test_veto_daily_loss_breached(self):
        ctx = _ctx(risk=_risk(daily_loss_breached=True))
        r = HardGatesGate().apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "daily_loss_breached"

    def test_veto_outside_time_window(self):
        cfg = _cfg(ENTRY_TIME_WINDOWS="9:25-10:00")
        snap = _snap(timestamp=datetime(2024, 8, 1, 11, 0))
        ctx = _ctx(snap=snap, cfg=cfg)
        r = HardGatesGate().apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert "time_window" in r.reason

    def test_pass_inside_time_window(self):
        cfg = _cfg(ENTRY_TIME_WINDOWS="9:25-10:00")
        snap = _snap(timestamp=datetime(2024, 8, 1, 9, 30))
        ctx = _ctx(snap=snap, cfg=cfg)
        r = HardGatesGate().apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_pass_no_time_restriction(self):
        ctx = _ctx(snap=_snap(timestamp=datetime(2024, 8, 1, 14, 30)))
        r = HardGatesGate().apply(ctx)
        assert r.outcome == GateOutcome.PASS


# ===========================================================================
# Gate 2 — VotesGate
# ===========================================================================

class TestVotesGate:
    def test_pass_with_entry_votes(self):
        ctx = _ctx(votes=[_vote()])
        r = VotesGate().apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_veto_no_votes(self):
        ctx = _ctx(votes=[])
        r = VotesGate().apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "no_entry_votes"

    def test_veto_only_avoid_votes(self):
        avoid = _vote(direction=Direction.AVOID)
        ctx = _ctx(votes=[avoid])
        r = VotesGate().apply(ctx)
        assert r.outcome == GateOutcome.VETO

    def test_pass_with_pe_vote(self):
        ctx = _ctx(votes=[_vote(direction=Direction.PE)])
        r = VotesGate().apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_values_contain_total_votes(self):
        avoid = _vote(direction=Direction.AVOID)
        ctx = _ctx(votes=[avoid])
        r = VotesGate().apply(ctx)
        assert "total_votes" in r.values


# ===========================================================================
# Gate 3 — RegimeConfidenceGate
# ===========================================================================

class TestRegimeConfidenceGate:
    def test_pass_sufficient_confidence(self):
        ctx = _ctx(regime=_regime(confidence=0.80))
        r = RegimeConfidenceGate().apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_veto_low_confidence(self):
        ctx = _ctx(regime=_regime(confidence=0.50))
        r = RegimeConfidenceGate().apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "low_regime_confidence"
        assert r.values["confidence"] == pytest.approx(0.50)

    def test_pass_exactly_at_threshold(self):
        ctx = _ctx(regime=_regime(confidence=0.60))
        r = RegimeConfidenceGate().apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_pass_when_relaxed(self):
        ctx = _ctx(regime=_regime(confidence=0.10))
        r = RegimeConfidenceGate(relax=True).apply(ctx)
        assert r.outcome == GateOutcome.PASS


# ===========================================================================
# Gate 4 — DirectionGate
# ===========================================================================

def _make_direction_gate(*, consensus_vetoed=False, direction=Direction.CE):
    shadow_fn = MagicMock(return_value=(Direction.CE, "shadow_ce", 1.5))
    ml_hint_fn = MagicMock(return_value=(direction, 0.70))

    mock_consensus = MagicMock()
    mock_consensus.vetoed = consensus_vetoed
    mock_consensus.direction = None if consensus_vetoed else direction
    mock_consensus.veto_reason = "test_veto" if consensus_vetoed else ""
    mock_consensus.ce_score = 2.0
    mock_consensus.pe_score = 0.5
    mock_consensus.margin = 1.5
    mock_consensus.sources = {}

    consensus_fn = MagicMock(return_value=mock_consensus)
    ml_vote_selector = MagicMock(return_value=_vote())

    gate = DirectionGate(
        shadow_fn=shadow_fn,
        ml_hint_fn=ml_hint_fn,
        consensus_fn=consensus_fn,
        ml_entry_vote_selector=ml_vote_selector,
    )
    return gate, shadow_fn, consensus_fn


class TestDirectionGate:
    def test_pass_sets_ctx_direction(self):
        gate, _, _ = _make_direction_gate(direction=Direction.CE)
        ctx = _ctx(votes=[_vote(name="ML_ENTRY", confidence=0.80)])
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.PASS
        assert ctx.direction == Direction.CE

    def test_veto_no_ml_entry_vote(self):
        gate, _, _ = _make_direction_gate()
        ctx = _ctx(votes=[_vote(name="RULE_STRATEGY")])
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "no_ml_entry_vote"

    def test_veto_ml_confidence_below_bypass(self):
        gate, _, _ = _make_direction_gate()
        cfg = _cfg(CONSENSUS_BYPASS_MIN_CONFIDENCE="0.90")
        ctx = _ctx(
            votes=[_vote(name="ML_ENTRY", confidence=0.70)],
            cfg=cfg,
        )
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "ml_confidence_below_bypass"

    def test_veto_consensus_vetoed(self):
        gate, _, _ = _make_direction_gate(consensus_vetoed=True)
        ctx = _ctx(votes=[_vote(name="ML_ENTRY", confidence=0.80)])
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "direction_consensus_vetoed"

    def test_pass_pe_direction(self):
        gate, _, _ = _make_direction_gate(direction=Direction.PE)
        ctx = _ctx(votes=[_vote(name="ML_ENTRY", confidence=0.80)])
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.PASS
        assert ctx.direction == Direction.PE


# ===========================================================================
# Gate 5 — StrikeDepthGate
# ===========================================================================

def _make_strike_gate(*, vetoed=False, premium=100.0):
    def _apply_strike(vote, snap_, regime=""):
        if vetoed:
            vote.raw_signals["_strike_vetoed"] = True
            vote.raw_signals["_strike_veto_reason"] = "test_iv_reject"
        else:
            vote.proposed_strike = snap_.atm_strike
            vote.proposed_entry_premium = premium

    return StrikeDepthGate(apply_strike_fn=_apply_strike)


class TestStrikeDepthGate:
    def test_pass_sets_strike_and_premium(self):
        gate = _make_strike_gate(premium=150.0)
        ctx = _ctx(votes=[_vote()])
        ctx.direction = Direction.CE
        ctx.reset_candidate(_vote())
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.PASS
        assert ctx.strike == 50000
        assert ctx.premium == pytest.approx(150.0)

    def test_veto_strike_vetoed(self):
        gate = _make_strike_gate(vetoed=True)
        ctx = _ctx()
        ctx.direction = Direction.CE
        ctx.reset_candidate(_vote())
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "strike_vetoed"
        assert "max_premium" in r.values
        assert "decision_id" in r.values

    def test_veto_no_candidate(self):
        gate = _make_strike_gate()
        ctx = _ctx()
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "no_candidate"

    def test_veto_premium_exceeds_hard_cap(self):
        gate = _make_strike_gate(premium=600.0)
        cfg = _cfg(SMART_STRIKE_MAX_PREMIUM="500", SMART_STRIKE_HARD_PREMIUM_CAP="1")
        ctx = _ctx(cfg=cfg)
        ctx.direction = Direction.CE
        ctx.reset_candidate(_vote())
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.VETO
        assert r.reason == "premium_exceeds_hard_cap"
        assert r.values["premium"] == pytest.approx(600.0)
        assert r.values["max_premium"] == pytest.approx(500.0)

    def test_pass_premium_within_cap(self):
        gate = _make_strike_gate(premium=400.0)
        cfg = _cfg(SMART_STRIKE_MAX_PREMIUM="500", SMART_STRIKE_HARD_PREMIUM_CAP="1")
        ctx = _ctx(cfg=cfg)
        ctx.direction = Direction.CE
        ctx.reset_candidate(_vote())
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_pass_no_cap_when_max_premium_zero(self):
        gate = _make_strike_gate(premium=2000.0)
        cfg = _cfg(SMART_STRIKE_MAX_PREMIUM="0", SMART_STRIKE_HARD_PREMIUM_CAP="1")
        ctx = _ctx(cfg=cfg)
        ctx.direction = Direction.CE
        ctx.reset_candidate(_vote())
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_pass_no_cap_when_hard_cap_disabled(self):
        gate = _make_strike_gate(premium=2000.0)
        cfg = _cfg(SMART_STRIKE_MAX_PREMIUM="500", SMART_STRIKE_HARD_PREMIUM_CAP="0")
        ctx = _ctx(cfg=cfg)
        ctx.direction = Direction.CE
        ctx.reset_candidate(_vote())
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_direction_override_from_ctx(self):
        recorded = {}

        def _apply_strike(vote, snap_, regime=""):
            recorded["direction"] = vote.direction
            vote.proposed_strike = snap_.atm_strike
            vote.proposed_entry_premium = 100.0

        gate = StrikeDepthGate(apply_strike_fn=_apply_strike)
        ctx = _ctx()
        v = _vote(direction=Direction.CE)
        ctx.reset_candidate(v)
        ctx.direction = Direction.PE  # set after reset so gate sees it
        gate.apply(ctx)
        assert recorded["direction"] == Direction.PE


# ===========================================================================
# Gate 6 — EntryPolicyGate
# ===========================================================================

def _make_policy_gate(*, allowed: bool = True, reason: str = "ok", score: float = 0.80):
    from strategy_app.policy.entry_policy import EntryPolicyDecision

    def _policy(vote, snap_, regime_, risk_):
        if allowed:
            return EntryPolicyDecision.allow(reason, score=score, checks={"c": "ok"})
        return EntryPolicyDecision.block(reason, checks={"c": "fail"})

    return EntryPolicyGate(policy_fn=_policy)


class TestEntryPolicyGate:
    def test_pass_allowed(self):
        gate = _make_policy_gate(allowed=True)
        ctx = _ctx()
        ctx.reset_candidate(_vote())
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_skip_not_allowed(self):
        gate = _make_policy_gate(allowed=False, reason="iv_too_high")
        ctx = _ctx()
        ctx.reset_candidate(_vote())
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.SKIP_CANDIDATE
        assert r.reason == "entry_policy_blocked"
        assert r.values["policy_reason"] == "iv_too_high"

    def test_skip_no_candidate(self):
        gate = _make_policy_gate()
        ctx = _ctx()
        r = gate.apply(ctx)
        assert r.outcome == GateOutcome.SKIP_CANDIDATE

    def test_policy_annotations_written_to_raw_signals(self):
        gate = _make_policy_gate(allowed=True, score=0.75)
        ctx = _ctx()
        v = _vote()
        ctx.reset_candidate(v)
        gate.apply(ctx)
        assert v.raw_signals["_policy_allowed"] is True
        assert v.raw_signals["_policy_score"] == pytest.approx(0.75, abs=0.01)


# ===========================================================================
# Gate 7 — ConfidenceGate
# ===========================================================================

class TestConfidenceGate:
    def test_pass_above_threshold(self):
        ctx = _ctx(cfg=_cfg(STRATEGY_MIN_CONFIDENCE="0.65"))
        ctx.reset_candidate(_vote(confidence=0.80))
        r = ConfidenceGate().apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_skip_below_threshold(self):
        ctx = _ctx(cfg=_cfg(STRATEGY_MIN_CONFIDENCE="0.65"))
        ctx.reset_candidate(_vote(confidence=0.50))
        r = ConfidenceGate().apply(ctx)
        assert r.outcome == GateOutcome.SKIP_CANDIDATE
        assert r.reason == "below_min_confidence"
        assert r.values["confidence"] == pytest.approx(0.50)

    def test_pass_exactly_at_threshold(self):
        ctx = _ctx(cfg=_cfg(STRATEGY_MIN_CONFIDENCE="0.65"))
        ctx.reset_candidate(_vote(confidence=0.65))
        r = ConfidenceGate().apply(ctx)
        assert r.outcome == GateOutcome.PASS

    def test_skip_no_candidate(self):
        ctx = _ctx()
        r = ConfidenceGate().apply(ctx)
        assert r.outcome == GateOutcome.SKIP_CANDIDATE


# ===========================================================================
# evaluate_v2() integration
# ===========================================================================

class TestEvaluateV2:
    def _make_pass_gates(self):
        from strategy_app.engines.entry_pipeline_contracts import Gate, GateResult

        class _P(Gate):
            name = "pass"
            def apply(self, ctx: EntryContext) -> GateResult:
                return GateResult.ok()

        return [_P()]

    def _make_veto_gates(self):
        from strategy_app.engines.entry_pipeline_contracts import Gate, GateResult

        class _V(Gate):
            name = "veto"
            def apply(self, ctx: EntryContext) -> GateResult:
                return GateResult.veto("forced_veto")

        return [_V()]

    def test_returns_signal_on_all_pass(self):
        mock_signal = MagicMock()
        build_fn = MagicMock(return_value=mock_signal)

        ctx = _ctx(votes=[_vote()])
        result = evaluate_v2(ctx=ctx, gates=self._make_pass_gates(), build_signal_fn=build_fn)

        assert result is mock_signal
        build_fn.assert_called_once()

    def test_returns_none_on_veto(self):
        build_fn = MagicMock()
        ctx = _ctx(votes=[_vote()])
        result = evaluate_v2(ctx=ctx, gates=self._make_veto_gates(), build_signal_fn=build_fn)
        assert result is None
        build_fn.assert_not_called()

    def test_returns_none_on_empty_votes(self):
        build_fn = MagicMock()
        ctx = _ctx(votes=[])
        result = evaluate_v2(ctx=ctx, gates=self._make_pass_gates(), build_signal_fn=build_fn)
        assert result is None
        build_fn.assert_not_called()

    def test_skip_advances_to_next_candidate(self):
        from strategy_app.engines.entry_pipeline_contracts import Gate, GateResult

        call_count = [0]

        class _SkipFirst(Gate):
            name = "skip_first"
            def apply(self, ctx: EntryContext) -> GateResult:
                call_count[0] += 1
                if call_count[0] == 1:
                    return GateResult.skip("first_skipped")
                return GateResult.ok()

        mock_signal = MagicMock()
        build_fn = MagicMock(return_value=mock_signal)

        v1 = _vote(name="ML_ENTRY", confidence=0.90)
        v2 = _vote(name="RULE_STRAT", confidence=0.70)
        ctx = _ctx(votes=[v1, v2])

        result = evaluate_v2(ctx=ctx, gates=[_SkipFirst()], build_signal_fn=build_fn)
        assert result is mock_signal
        assert call_count[0] == 2

    def test_custom_rank_fn_respected(self):
        """rank_votes_fn controls order; test that build is called with the result of last reset."""
        order = []

        def _rank(votes):
            sorted_votes = sorted(votes, key=lambda v: v.strategy_name)
            return sorted_votes

        from strategy_app.engines.entry_pipeline_contracts import Gate, GateResult

        class _RecordGate(Gate):
            name = "record"
            def apply(self, ctx: EntryContext) -> GateResult:
                if ctx.candidate:
                    order.append(ctx.candidate.strategy_name)
                return GateResult.veto("stop")

        v1 = _vote(name="Z_STRAT", confidence=0.80)
        v2 = _vote(name="A_STRAT", confidence=0.80)
        ctx = _ctx(votes=[v1, v2])

        evaluate_v2(ctx=ctx, gates=[_RecordGate()], build_signal_fn=MagicMock(), rank_votes_fn=_rank)
        assert order[0] == "A_STRAT"
