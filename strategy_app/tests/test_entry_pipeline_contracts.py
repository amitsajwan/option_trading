"""Unit tests for entry_pipeline_contracts.

Covers: GateOutcome, GateResult factories, GateTrace, EntryContext.reset_candidate,
and run_chain() PASS / VETO / SKIP_CANDIDATE behaviours.

All tests are pure data — no engine boot, no Redis, no Mongo.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from strategy_app.engines.entry_pipeline_contracts import (
    EntryContext,
    Gate,
    GateOutcome,
    GateResult,
    GateTrace,
    run_chain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(**kwargs) -> EntryContext:
    snap = MagicMock()
    snap.is_valid_entry_phase = True
    snap.timestamp = datetime(2024, 8, 1, 9, 30)
    regime = MagicMock()
    regime.confidence = 0.80
    risk = MagicMock()
    risk.daily_loss_breached = False
    cfg = MagicMock()
    cfg.entry_time_windows = ()
    cfg.regime_allowed_tags = frozenset()
    cfg.min_confidence = 0.65
    cfg.bypass_min_confidence = 0.65
    cfg.regime_min_confidence = 0.60
    cfg.time_window_allows = lambda m: True
    cfg.regime_tag_allows = lambda t: True
    return EntryContext(
        snap=snap,
        regime=regime,
        risk=risk,
        votes=kwargs.get("votes", []),
        config=cfg,
    )


class _PassGate(Gate):
    name = "pass_gate"

    def apply(self, ctx: EntryContext) -> GateResult:
        return GateResult.ok()


class _VetoGate(Gate):
    name = "veto_gate"

    def __init__(self, reason: str = "test_veto") -> None:
        self._reason = reason

    def apply(self, ctx: EntryContext) -> GateResult:
        return GateResult.veto(self._reason, flag=True)


class _SkipGate(Gate):
    name = "skip_gate"

    def apply(self, ctx: EntryContext) -> GateResult:
        return GateResult.skip("test_skip", score=0.3)


class _CountingGate(Gate):
    name = "counting_gate"

    def __init__(self) -> None:
        self.call_count = 0

    def apply(self, ctx: EntryContext) -> GateResult:
        self.call_count += 1
        return GateResult.ok()


# ---------------------------------------------------------------------------
# GateOutcome
# ---------------------------------------------------------------------------

def test_gate_outcome_values():
    assert GateOutcome.PASS.value == "pass"
    assert GateOutcome.VETO.value == "veto"
    assert GateOutcome.SKIP_CANDIDATE.value == "skip_candidate"


# ---------------------------------------------------------------------------
# GateResult factories
# ---------------------------------------------------------------------------

def test_gate_result_ok():
    r = GateResult.ok()
    assert r.outcome == GateOutcome.PASS
    assert r.reason == ""
    assert r.values == {}


def test_gate_result_veto():
    r = GateResult.veto("no_regime", conf=0.5, required=0.6)
    assert r.outcome == GateOutcome.VETO
    assert r.reason == "no_regime"
    assert r.values["conf"] == 0.5
    assert r.values["required"] == 0.6


def test_gate_result_skip():
    r = GateResult.skip("low_conf", confidence=0.3)
    assert r.outcome == GateOutcome.SKIP_CANDIDATE
    assert r.reason == "low_conf"
    assert r.values["confidence"] == 0.3


def test_gate_result_is_frozen():
    r = GateResult.ok()
    with pytest.raises((AttributeError, TypeError)):
        r.outcome = GateOutcome.VETO  # type: ignore[misc]


# ---------------------------------------------------------------------------
# GateTrace
# ---------------------------------------------------------------------------

def test_gate_trace_fields():
    t = GateTrace(gate_name="TestGate", outcome=GateOutcome.VETO, reason="x", values={"k": 1})
    assert t.gate_name == "TestGate"
    assert t.outcome == GateOutcome.VETO
    assert t.reason == "x"
    assert t.values == {"k": 1}


# ---------------------------------------------------------------------------
# EntryContext
# ---------------------------------------------------------------------------

def test_entry_context_decision_id_is_set():
    ctx = _make_ctx()
    assert ctx.decision_id
    assert len(ctx.decision_id) == 8


def test_entry_context_decision_ids_unique():
    ids = {_make_ctx().decision_id for _ in range(20)}
    assert len(ids) > 1


def test_entry_context_defaults():
    ctx = _make_ctx()
    assert ctx.candidate is None
    assert ctx.direction is None
    assert ctx.strike is None
    assert ctx.premium is None
    assert ctx.lots is None
    assert ctx.trace == []


def test_entry_context_reset_candidate():
    ctx = _make_ctx()
    vote = MagicMock()
    ctx.direction = MagicMock()
    ctx.strike = 50000
    ctx.premium = 100.0
    ctx.lots = 2

    ctx.reset_candidate(vote)

    assert ctx.candidate is vote
    assert ctx.direction is None
    assert ctx.strike is None
    assert ctx.premium is None
    assert ctx.lots is None


def test_entry_context_reset_preserves_trace():
    ctx = _make_ctx()
    ctx.trace.append(GateTrace("g", GateOutcome.PASS))
    vote = MagicMock()
    ctx.reset_candidate(vote)
    assert len(ctx.trace) == 1


# ---------------------------------------------------------------------------
# run_chain — PASS path
# ---------------------------------------------------------------------------

def test_run_chain_all_pass():
    ctx = _make_ctx()
    gates = [_PassGate(), _PassGate(), _PassGate()]
    result = run_chain(ctx, gates)
    assert result.outcome == GateOutcome.PASS
    assert len(ctx.trace) == 3


def test_run_chain_empty_gates():
    ctx = _make_ctx()
    result = run_chain(ctx, [])
    assert result.outcome == GateOutcome.PASS
    assert ctx.trace == []


# ---------------------------------------------------------------------------
# run_chain — VETO stops immediately
# ---------------------------------------------------------------------------

def test_run_chain_veto_stops_pipeline():
    ctx = _make_ctx()
    after = _CountingGate()
    gates = [_PassGate(), _VetoGate("blocked"), after]
    result = run_chain(ctx, gates)

    assert result.outcome == GateOutcome.VETO
    assert result.reason == "blocked"
    assert after.call_count == 0
    assert len(ctx.trace) == 2  # PassGate + VetoGate, not AfterGate


def test_run_chain_veto_traces_reason():
    ctx = _make_ctx()
    run_chain(ctx, [_VetoGate("the_reason")])
    assert ctx.trace[0].reason == "the_reason"
    assert ctx.trace[0].outcome == GateOutcome.VETO


# ---------------------------------------------------------------------------
# run_chain — SKIP_CANDIDATE stops immediately
# ---------------------------------------------------------------------------

def test_run_chain_skip_stops_pipeline():
    ctx = _make_ctx()
    after = _CountingGate()
    gates = [_PassGate(), _SkipGate(), after]
    result = run_chain(ctx, gates)

    assert result.outcome == GateOutcome.SKIP_CANDIDATE
    assert after.call_count == 0


def test_run_chain_skip_traces():
    ctx = _make_ctx()
    run_chain(ctx, [_SkipGate()])
    assert ctx.trace[0].outcome == GateOutcome.SKIP_CANDIDATE
    assert ctx.trace[0].reason == "test_skip"


# ---------------------------------------------------------------------------
# run_chain — trace accumulation across multiple runs
# ---------------------------------------------------------------------------

def test_run_chain_trace_accumulates_across_calls():
    ctx = _make_ctx()
    run_chain(ctx, [_PassGate()])
    run_chain(ctx, [_PassGate()])
    assert len(ctx.trace) == 2


def test_run_chain_values_forwarded_in_trace():
    ctx = _make_ctx()

    class _ValGate(Gate):
        name = "val_gate"
        def apply(self, c: EntryContext) -> GateResult:
            return GateResult.veto("reason", key="val", num=42)

    run_chain(ctx, [_ValGate()])
    assert ctx.trace[0].values["key"] == "val"
    assert ctx.trace[0].values["num"] == 42
