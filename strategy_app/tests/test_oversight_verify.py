"""Anti-hallucination verification: LLM verdicts that contradict facts get downgraded."""

from __future__ import annotations

from strategy_app.brain.oversight import OversightVerdict, verify_verdict


def test_bullish_lean_on_down_tape_is_killed():
    facts = {"fut_vs_prev_close_pct": -0.006, "fut_price": 54000}
    v, flags = verify_verdict(OversightVerdict(direction_lean="CE", lean_confidence=0.8, posture="trend_up"), facts)
    assert v.direction_lean == "none" and v.lean_confidence == 0.0
    assert v.posture == "choppy"
    assert "lean_CE_contradicts_downmove" in flags


def test_bearish_lean_on_up_tape_is_killed():
    facts = {"fut_vs_prev_close_pct": 0.006, "fut_price": 54000}
    v, flags = verify_verdict(OversightVerdict(direction_lean="PE", lean_confidence=0.8), facts)
    assert v.direction_lean == "none"
    assert "lean_PE_contradicts_upmove" in flags


def test_consistent_lean_is_kept():
    facts = {"fut_vs_prev_close_pct": -0.006, "fut_price": 54000}
    v, flags = verify_verdict(OversightVerdict(direction_lean="PE", lean_confidence=0.7, posture="trend_down"), facts)
    assert v.direction_lean == "PE" and v.lean_confidence == 0.7
    assert flags == []


def test_overconfidence_on_flat_capped():
    facts = {"fut_vs_prev_close_pct": 0.0003, "fut_price": 54000}
    v, flags = verify_verdict(OversightVerdict(direction_lean="CE", lean_confidence=0.9), facts)
    assert v.lean_confidence == 0.5
    assert "overconfident_on_flat_tape" in flags


def test_implausible_levels_dropped():
    facts = {"fut_vs_prev_close_pct": 0.0, "fut_price": 54000, "week_low": 53000, "week_high": 55000}
    v, flags = verify_verdict(OversightVerdict(key_levels=(54100.0, 99999.0)), facts)
    assert 99999.0 not in v.key_levels and 54100.0 in v.key_levels
    assert "dropped_implausible_levels" in flags


def test_missing_facts_safe():
    v, flags = verify_verdict(OversightVerdict(direction_lean="CE", lean_confidence=0.7), {})
    assert v.direction_lean == "CE"  # no facts to contradict → unchanged
    assert flags == []
