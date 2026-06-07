"""Tests for deterministic post-trade reflection (Phase 2) — no LLM, pure."""

from __future__ import annotations

import json

from strategy_app.brain.reflection import (
    ClosedTrade,
    ExecQualityResult,
    LossTag,
    autopsy,
    execution_quality,
    reflect,
)
from strategy_app.senses import SenseVerdict


def _trade(**kw) -> ClosedTrade:
    base = dict(
        direction="CE",
        net_pnl_frac=-0.10,
        cost_frac=0.013,
        mfe_frac=0.0,
        target_frac=0.40,
        stop_frac=0.20,
        mae_frac=-0.20,
        bars_held=5,
        exit_reason="stop_loss",
        entry_verdicts={},
    )
    base.update(kw)
    return ClosedTrade(**base)


# ─────────────────────────── loss classification ────────────────────────────

class TestAutopsyTags:
    def test_win_has_no_tag(self):
        r = autopsy(_trade(net_pnl_frac=0.15, mfe_frac=0.30))
        assert r.is_loss is False
        assert r.tag is None
        assert r.needs_reasoning is False
        # giveback is still recorded for wins
        assert r.evidence["giveback_frac"] >= 0

    def test_cost_miss(self):
        # price made +0.5% but cost 1.3% => net negative
        r = autopsy(_trade(net_pnl_frac=-0.008, cost_frac=0.013, mfe_frac=0.05))
        assert r.tag == LossTag.COST_MISS.value
        assert r.needs_reasoning is False
        assert r.evidence["gross_pnl_frac"] > 0

    def test_exit_miss_gave_back_winner(self):
        # reached 90% of the 0.40 target, then closed red
        r = autopsy(_trade(net_pnl_frac=-0.05, cost_frac=0.0, mfe_frac=0.36,
                           target_frac=0.40, exit_reason="trail_stop"))
        assert r.tag == LossTag.EXIT_MISS.value
        assert r.needs_reasoning is False

    def test_direction_miss_wrong_side(self):
        # barely went our way (mfe 0.05 of 0.40 target), big adverse
        r = autopsy(_trade(net_pnl_frac=-0.20, cost_frac=0.0, mfe_frac=0.05,
                           target_frac=0.40, mae_frac=-0.22))
        assert r.tag == LossTag.DIRECTION_MISS.value
        assert r.needs_reasoning is False

    def test_entry_miss_marginal_senses(self):
        # mid MFE (between direction & exit thresholds) but senses were marginal
        verdicts = {
            "move": SenseVerdict(sense="move", verdict="loaded", confidence=0.30),
            "direction": SenseVerdict(sense="direction", verdict="CE", confidence=0.40),
        }
        r = autopsy(_trade(net_pnl_frac=-0.10, cost_frac=0.0, mfe_frac=0.18,
                           target_frac=0.40, entry_verdicts=verdicts))
        assert r.tag == LossTag.ENTRY_MISS.value
        assert r.evidence["marginal_entry"] is True

    def test_entry_miss_on_conflict(self):
        verdicts = {
            "move": SenseVerdict(sense="move", verdict="loaded", confidence=0.9),
            "conflict": SenseVerdict(sense="conflict", verdict="ofi_vs_price", confidence=0.7),
        }
        r = autopsy(_trade(net_pnl_frac=-0.10, cost_frac=0.0, mfe_frac=0.18,
                           target_frac=0.40, entry_verdicts=verdicts))
        assert r.tag == LossTag.ENTRY_MISS.value
        assert r.evidence["conflict_present"] is True

    def test_noise_small_loss_no_reasoning(self):
        # strong senses, mid MFE, tiny loss within noise band (<=20% of stop)
        verdicts = {
            "move": SenseVerdict(sense="move", verdict="loaded", confidence=0.9),
            "direction": SenseVerdict(sense="direction", verdict="CE", confidence=0.8),
        }
        r = autopsy(_trade(net_pnl_frac=-0.03, cost_frac=0.0, mfe_frac=0.18,
                           target_frac=0.40, stop_frac=0.20, entry_verdicts=verdicts))
        assert r.tag == LossTag.NOISE.value
        assert r.needs_reasoning is False

    def test_absent_verdicts_is_not_entry_miss(self):
        # Production case: no entry verdicts captured. A mid-MFE loss must fall to
        # NOISE+needs_reasoning (hand to LLM), NOT be mis-tagged entry_miss.
        r = autopsy(_trade(net_pnl_frac=-0.12, cost_frac=0.0, mfe_frac=0.18,
                           target_frac=0.40, stop_frac=0.20, entry_verdicts={}))
        assert r.tag == LossTag.NOISE.value
        assert r.needs_reasoning is True
        assert r.evidence["marginal_entry"] is False

    def test_ambiguous_loss_needs_reasoning(self):
        # strong senses, mid MFE, sizeable loss that fits no clean bucket => LLM
        verdicts = {
            "move": SenseVerdict(sense="move", verdict="loaded", confidence=0.9),
            "direction": SenseVerdict(sense="direction", verdict="CE", confidence=0.8),
        }
        r = autopsy(_trade(net_pnl_frac=-0.15, cost_frac=0.0, mfe_frac=0.18,
                           target_frac=0.40, stop_frac=0.20, entry_verdicts=verdicts))
        assert r.tag == LossTag.NOISE.value
        assert r.needs_reasoning is True


# ─────────────────────────── verdict adapter ────────────────────────────────

class TestVerdictView:
    def test_accepts_trace_dicts(self):
        # entry verdicts persisted as dicts (from the trace) must work too
        verdicts = {
            "move": {"verdict": "loaded", "confidence": 0.30},
            "direction": {"verdict": "unclear", "confidence": None},
        }
        r = autopsy(_trade(net_pnl_frac=-0.10, mfe_frac=0.18, target_frac=0.40,
                           entry_verdicts=verdicts))
        assert r.evidence["marginal_entry"] is True

    def test_gross_and_loss_helpers(self):
        t = _trade(net_pnl_frac=-0.008, cost_frac=0.013)
        assert round(t.gross_pnl_frac, 6) == 0.005
        assert t.is_loss is True


# ─────────────────────────── ClosedTrade.from_position ──────────────────────

class TestFromPosition:
    def test_duck_typed_adapter(self):
        class FakePosition:
            direction = "PE"
            pnl_pct = -0.12
            mfe_pct = 0.04
            mae_pct = -0.13
            bars_held = 7

        t = ClosedTrade.from_position(
            FakePosition(), cost_frac=0.013, target_frac=0.40, stop_frac=0.20,
            exit_reason="stop_loss",
        )
        assert t.direction == "PE"
        assert t.net_pnl_frac == -0.12
        assert t.mfe_frac == 0.04
        assert autopsy(t).tag == LossTag.DIRECTION_MISS.value


# ─────────────────────────── execution quality ──────────────────────────────

class TestExecutionQuality:
    def test_ok_when_cost_small_vs_edge(self):
        r = execution_quality(slippage_frac=0.002, charges_frac=0.001, edge_frac=0.05)
        assert isinstance(r, ExecQualityResult)
        assert r.flag == "ok"
        assert r.overpaid is False

    def test_high_cost(self):
        # cost 0.013 / edge 0.02 = 0.65 -> high_cost (>=0.5, <1.0)
        r = execution_quality(slippage_frac=0.008, charges_frac=0.005, edge_frac=0.02)
        assert r.flag == "high_cost"
        assert r.overpaid is True

    def test_cost_exceeds_edge(self):
        r = execution_quality(slippage_frac=0.0075, charges_frac=0.009, edge_frac=0.01)
        assert r.flag == "cost_exceeds_edge"
        assert r.cost_to_edge is not None and r.cost_to_edge >= 1.0

    def test_no_edge(self):
        r = execution_quality(slippage_frac=0.005, charges_frac=0.005, edge_frac=0.0)
        assert r.flag == "cost_exceeds_edge"
        assert r.cost_to_edge is None


# ─────────────────────────── reflect() journal record ───────────────────────

class TestReflect:
    def test_combined_record_is_json_safe(self):
        verdicts = {"move": SenseVerdict(sense="move", verdict="loaded", confidence=0.9)}
        rec = reflect(
            _trade(net_pnl_frac=-0.008, cost_frac=0.013, mfe_frac=0.05,
                   entry_verdicts=verdicts),
            edge_frac=0.02,
        )
        # round-trips through json => fully serialisable
        json.loads(json.dumps(rec))
        assert rec["autopsy"]["tag"] == LossTag.COST_MISS.value
        assert rec["execution"]["flag"] in {"ok", "high_cost", "cost_exceeds_edge"}
        assert "needs_reasoning" in rec["autopsy"]

    def test_ambiguous_flags_for_llm(self):
        verdicts = {
            "move": SenseVerdict(sense="move", verdict="loaded", confidence=0.9),
            "direction": SenseVerdict(sense="direction", verdict="CE", confidence=0.8),
        }
        rec = reflect(
            _trade(net_pnl_frac=-0.15, cost_frac=0.0, mfe_frac=0.18,
                   target_frac=0.40, stop_frac=0.20, entry_verdicts=verdicts),
            edge_frac=0.05,
        )
        assert rec["autopsy"]["needs_reasoning"] is True
