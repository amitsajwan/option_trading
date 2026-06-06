"""Structure sense + its brain integration (conflict fakeout, breakout quality bonus)."""
from __future__ import annotations

from strategy_app.brain.decision_brain import analyze_conflicts, assess_opportunity
from strategy_app.senses import SenseVerdict
from strategy_app.senses.structure import StructureSense


def _ctx(**over):
    base = {"struct_breakout": "none", "struct_fakeout": False,
            "struct_position": "inside", "struct_trend": "choppy",
            "day_high": 54200.0, "day_low": 53800.0}
    base.update(over)
    return base


def _v(sense, verdict, conf=0.6, value=None, **ev):
    return SenseVerdict(sense=sense, verdict=verdict, confidence=conf, value=value, evidence=ev)


# ---- StructureSense verdicts (the trader's read) ----

def test_structure_abstains_without_inputs():
    assert StructureSense().evaluate({}).is_abstain


def test_structure_fakeout_is_a_trap():
    v = StructureSense().evaluate(_ctx(struct_fakeout=True))
    assert v.verdict == "fakeout" and v.value == -1.0


def test_structure_breakout_is_release_and_trend_aligned_is_more_confident():
    aligned = StructureSense().evaluate(_ctx(struct_breakout="up", struct_trend="up"))
    misaligned = StructureSense().evaluate(_ctx(struct_breakout="up", struct_trend="down"))
    assert aligned.verdict == "breakout" and misaligned.verdict == "breakout"
    assert aligned.confidence > misaligned.confidence
    assert aligned.evidence["trend_aligned"] is True


def test_structure_at_extreme_and_coiling():
    assert StructureSense().evaluate(_ctx(struct_position="near_high")).verdict == "at_extreme"
    assert StructureSense().evaluate(_ctx()).verdict == "coiling"   # inside, no breakout


def test_structure_records_breakout_direction_as_evidence_only():
    # direction is recorded for research but the verdict is NOT a side
    v = StructureSense().evaluate(_ctx(struct_breakout="down", struct_trend="down"))
    assert v.verdict == "breakout"            # not "PE"/"down" — direction-agnostic verdict
    assert v.evidence["breakout"] == "down"   # the side is preserved as evidence


# ---- brain integration ----

def _loaded_verdicts(**over):
    v = {
        "move": _v("move", "loaded", compression=True, oi_build=True, velocity=False, volume=False,
                   last_bar_return=2.0, expected_move_pt=117.0, prob_100=0.49, prob_200=0.11),
        "direction": _v("direction", "CE", conf=1.0),
        "flow": _v("flow", "neutral", net_ofi=0.0),
        "destination": _v("destination", "room", value=2.3, space_to_move_ratio=2.3),
        "cost_ev": _v("cost_ev", "+ev", gross_if_right_pct=0.0468, gross_if_wrong_pct=-0.0518, cost_pct=0.0109),
    }
    v.update(over)
    return v


def test_conflict_fakeout_blocks_loaded_spring():
    v = _loaded_verdicts(structure=_v("structure", "fakeout", value=-1.0))
    c = analyze_conflicts(v)
    assert "loaded_into_fakeout" in c.conflicts and c.action == "WAIT"


def test_no_fakeout_conflict_on_breakout():
    v = _loaded_verdicts(structure=_v("structure", "breakout", value=1.0))
    assert "loaded_into_fakeout" not in analyze_conflicts(v).conflicts


def test_structure_quality_is_neutral_except_fakeout_penalty():
    # DATA-DRIVEN (8 live days, n=24): breakout did NOT beat coiling, so quality is NOT
    # biased by breakout. Only a fakeout (trap) is penalised.
    breakout = assess_opportunity(_loaded_verdicts(structure=_v("structure", "breakout", value=1.0)), gate_p=1.0)
    coiling = assess_opportunity(_loaded_verdicts(structure=_v("structure", "coiling", value=0.3)), gate_p=1.0)
    at_extreme = assess_opportunity(_loaded_verdicts(structure=_v("structure", "at_extreme", value=0.0)), gate_p=1.0)
    fakeout = assess_opportunity(_loaded_verdicts(structure=_v("structure", "fakeout", value=-1.0)), gate_p=1.0)
    assert breakout.quality == coiling.quality == at_extreme.quality   # no unproven bias
    assert fakeout.quality < breakout.quality                          # trap still down-ranked
    assert breakout.evidence["structure"] == "breakout"
