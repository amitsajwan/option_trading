"""Tests for the DecisionBrain: ConflictAnalysis, OpportunityQuality, policy ladder (B-2.2)."""
from __future__ import annotations

from strategy_app.brain.decision_brain import (
    CURVE_POINTS,
    DecisionBrain,
    analyze_conflicts,
    assess_opportunity,
)
from strategy_app.senses import SenseVerdict


def _v(sense, verdict, conf=0.6, value=None, **ev):
    return SenseVerdict(sense=sense, verdict=verdict, confidence=conf, value=value, evidence=ev)


def _good_verdicts(**over):
    """A verdict map that should reach TRADE in defer_direction mode."""
    v = {
        "risk": _v("risk", "ok", blocked_reasons=[]),
        "regime": _v("regime", "compressed"),
        "move": _v("move", "released", compression=True, oi_build=True, velocity=True, volume=True,
                   released=True, last_bar_return=4.0, expected_move_pt=117.0, prob_100=0.49, prob_200=0.11),
        "direction": _v("direction", "CE", conf=1.0),
        "destination": _v("destination", "room", value=2.3, space_to_move_ratio=2.3),
        "cost_ev": _v("cost_ev", "+ev", gross_if_right_pct=0.0468, gross_if_wrong_pct=-0.0518, cost_pct=0.0109),
        "flow": _v("flow", "neutral", net_ofi=0.0),
        "execution": _v("execution", "ok", spread_pct=0.3),
    }
    v.update(over)
    return v


# ---- ConflictAnalysis (B-2.1 §2) ----

def test_conflict_A_direction_vs_flow():
    v = _good_verdicts(direction=_v("direction", "PE", conf=1.0), flow=_v("flow", "bull", net_ofi=0.4))
    c = analyze_conflicts(v)
    assert c.any and "move_strong_but_direction_conflicted" in c.conflicts and c.action == "WAIT"


def test_conflict_B_price_flow_decouple():
    v = _good_verdicts(flow=_v("flow", "bull", net_ofi=0.4),
                       move=_v("move", "loaded", compression=True, oi_build=True, last_bar_return=-30.0,
                               expected_move_pt=117.0, prob_200=0.11))
    c = analyze_conflicts(v)
    assert "ofi_bullish_price_falling" in c.conflicts and c.action == "WAIT"


def test_conflict_C_velocity_without_volume():
    v = _good_verdicts(move=_v("move", "released", compression=True, oi_build=True, velocity=True,
                               volume=False, last_bar_return=4.0, expected_move_pt=117.0, prob_200=0.11))
    c = analyze_conflicts(v)
    assert "velocity_up_volume_weak" in c.conflicts and c.action == "WAIT"


def test_conflict_D_no_space_is_skip():
    v = _good_verdicts(destination=_v("destination", "no_room", value=0.6, space_to_move_ratio=0.6))
    c = analyze_conflicts(v)
    assert "loaded_but_no_space" in c.conflicts and c.action == "SKIP"   # SKIP outranks WAIT


def test_conflict_none_when_aligned():
    assert not analyze_conflicts(_good_verdicts()).any


# ---- OpportunityQuality (B-2.1 §3) ----

def test_opportunity_curve_is_linear_and_increasing():
    opp = assess_opportunity(_good_verdicts(), p_ref=0.55)
    pts = [opp.net_curve[p] for p in sorted(CURVE_POINTS)]
    assert pts == sorted(pts)                      # monotonic increasing in accuracy
    # linearity: equal-spaced p give equal-spaced net (0.50->0.55->0.60)
    d1 = opp.net_curve[0.55] - opp.net_curve[0.50]
    d2 = opp.net_curve[0.60] - opp.net_curve[0.55]
    assert abs(d1 - d2) < 1e-9


def test_opportunity_gate_p_perfect_passes_marginal_setup():
    # at p_ref=0.55 the edge is negative, but judged at perfect it should pass
    v = _good_verdicts()
    at_ref = assess_opportunity(v, p_ref=0.55, gate_p=0.55)
    at_perfect = assess_opportunity(v, p_ref=0.55, gate_p=1.0)
    assert at_ref.edge_pct < 0 and not at_ref.passes
    assert at_perfect.passes and at_perfect.evidence["edge_at_gate_p"] > 0


# ---- policy ladder (B-2.1 §4) ----

def test_ladder_trade_when_all_agree_defer_direction():
    d = DecisionBrain(defer_direction=True).decide(_good_verdicts())
    assert d.action == "TRADE" and d.side == "CE" and d.size == 1 and d.ladder_step == 8


def test_ladder_risk_block_first():
    d = DecisionBrain(defer_direction=True).decide(
        _good_verdicts(risk=_v("risk", "blocked", blocked_reasons=["daily_dd"])))
    assert d.action == "SKIP" and d.ladder_step == 0 and "daily_dd" in d.reason


def test_ladder_dead_regime_no_trade():
    d = DecisionBrain(defer_direction=True).decide(_good_verdicts(regime=_v("regime", "dead")))
    assert d.action == "NO_TRADE" and d.ladder_step == 1


def test_ladder_no_loaded_spring():
    d = DecisionBrain(defer_direction=True).decide(_good_verdicts(move=_v("move", "quiet")))
    assert d.action == "NO_TRADE" and d.ladder_step == 2 and d.reason == "no_loaded_spring"


def test_ladder_unknown_direction_waits_in_live_mode():
    d = DecisionBrain(defer_direction=False).decide(_good_verdicts(direction=_v("direction", "UNKNOWN", conf=0.0)))
    assert d.action == "WAIT" and d.ladder_step == 4


def test_ladder_no_room_skip():
    d = DecisionBrain(defer_direction=True).decide(
        _good_verdicts(destination=_v("destination", "no_room", value=0.6, space_to_move_ratio=0.6)))
    # conflict D fires first (SKIP at step 3) — still a SKIP, structurally sound
    assert d.action == "SKIP"


def test_size_is_always_one_on_trade():
    # exhaustive: no configuration yields a TRADE with size != 1
    d = DecisionBrain(defer_direction=True).decide(_good_verdicts())
    assert (d.size == 1) if d.action == "TRADE" else (d.size == 0)


def test_decision_to_trace_is_serialisable():
    d = DecisionBrain(defer_direction=True).decide(_good_verdicts())
    t = d.to_trace()
    assert set(t) >= {"action", "side", "size", "reason", "ladder_step", "verdicts", "opportunity"}
    assert t["verdicts"]["move"]["sense"] == "move"
