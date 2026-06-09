"""Tests for the oversight 'trader brain' (sense/memory/calculator/reasoner). No network."""

from __future__ import annotations

import json

import pytest

from strategy_app.brain.oversight import MarketFacts, OversightVerdict, Scratchpad
from strategy_app.brain.oversight import brain as brain_mod
from strategy_app.brain.oversight.brain import OversightBrain
from strategy_app.brain.oversight.facts import location_zone
from strategy_app.brain.oversight.reasoner import _normalise, reason

_SNAP = {
    "payload": {"snapshot": {
        "timestamp": "2026-06-05T10:04:00",
        "trade_date": "2026-06-05",
        "futures_bar": {"fut_close": 54923, "fut_open": 54750},
        "session_levels": {
            "prev_day_high": 54750, "prev_day_low": 54126, "prev_day_close": 54651,
            "week_high": 55500, "week_low": 53287, "overnight_gap": 0.0023,
            "prev_day_pcr": 0.86, "prev_day_max_pain": 54400,
        },
        "chain_aggregates": {"pcr": 0.9, "max_pain": 54500, "ce_oi_top_strike": 55000, "pe_oi_top_strike": 54000},
        "vix_context": {"vix": 13.5},
    }}
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("BRAIN_OVERSIGHT_ENABLED", "BRAIN_LLM_API_KEY", "BRAIN_LLM_MODEL", "BRAIN_LLM_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    yield


# ─────────────────────────── SENSE / calculator ─────────────────────────────

class TestFacts:
    def test_extracts_and_computes(self):
        f = MarketFacts.from_snapshot(_SNAP)
        assert f.fut_price == 54923
        assert f.prev_day_high == 54750 and f.prev_day_low == 54126
        assert f.pcr == 0.9 and f.max_pain == 54500          # chain_aggregates preferred
        assert f.vix == 13.5
        assert f.dist_to_pdh == 173.0                         # extended above the high
        assert f.location_zone == "above_PDH"

    def test_falls_back_to_prev_day_pcr(self):
        snap = json.loads(json.dumps(_SNAP))
        snap["payload"]["snapshot"]["chain_aggregates"] = {}
        f = MarketFacts.from_snapshot(snap)
        assert f.pcr == 0.86 and f.max_pain == 54400          # session_levels fallback

    def test_prompt_dict_drops_nulls_and_takes_overlays(self):
        f = MarketFacts.from_snapshot(_SNAP, prior_fii_cr=-8776, events=["US CPI"])
        d = f.to_prompt_dict()
        assert d["prior_fii_cr"] == -8776 and d["events"] == ["US CPI"]
        assert all(v not in (None, "", []) for v in d.values())

    def test_zone_classification(self):
        assert location_zone(100, 99, 90) == "above_PDH"
        assert location_zone(95, 99, 90) == "mid_range"
        assert location_zone(89, 99, 90) == "below_PDL"


# ─────────────────────────── REASONER verdict ───────────────────────────────

class TestVerdict:
    def test_risk_state_pe_lean_vetoes_CE(self):
        v = OversightVerdict(direction_lean="PE", lean_confidence=0.7, risk_flag="reduce")
        st = v.to_risk_state()
        assert st["oversight_veto_side"] == "CE"
        assert st["oversight_risk_flag"] == "reduce"

    def test_low_confidence_lean_does_not_veto(self):
        v = OversightVerdict(direction_lean="PE", lean_confidence=0.4)
        assert v.to_risk_state()["oversight_veto_side"] == ""

    def test_stand_down(self):
        v = OversightVerdict(risk_flag="stand_down")
        assert v.to_risk_state()["oversight_risk_flag"] == "stand_down"

    def test_normalise_validates_enums(self):
        v = _normalise({"posture": "bananas", "direction_lean": "up", "risk_flag": "yolo", "lean_confidence": 5})
        assert v.posture == "unknown" and v.direction_lean == "none" and v.risk_flag == "normal"
        assert v.lean_confidence == 0.0  # none lean carries no confidence

    def test_normalise_good(self):
        v = _normalise({"posture": "trend_down", "direction_lean": "pe", "lean_confidence": 0.72,
                        "risk_flag": "reduce", "key_levels": [54750, 54126], "thesis": "bearish",
                        "reasoning": "below PDH, rejected"})
        assert v.posture == "trend_down" and v.direction_lean == "PE" and v.lean_confidence == 0.72
        assert v.key_levels == (54750.0, 54126.0)

    def test_reason_neutral_without_key(self):
        assert reason({}, {}, api_key="", base_url="x", model="m") == OversightVerdict()


# ─────────────────────────── MEMORY / scratchpad ────────────────────────────

class TestScratchpad:
    def test_add_persist_load_roundtrip(self, tmp_path):
        sp = Scratchpad(trade_date="2026-06-05")
        sp.add(time="10:04", verdict=OversightVerdict(posture="trend_down", direction_lean="PE",
                                                      lean_confidence=0.7, risk_flag="reduce",
                                                      thesis="bearish below PDH", reasoning="rejected high"),
               facts={"fut_price": 54923})
        p = tmp_path / "sp.json"
        sp.persist(p)
        sp2 = Scratchpad.load_or_new(p, "2026-06-05")
        assert sp2.thesis == "bearish below PDH"
        assert sp2.to_prompt_context()["recent_cycles"][0]["lean"] == "PE"

    def test_load_new_on_date_mismatch(self, tmp_path):
        sp = Scratchpad(trade_date="2026-06-05", thesis="x")
        p = tmp_path / "sp.json"; sp.persist(p)
        fresh = Scratchpad.load_or_new(p, "2026-06-08")  # different day
        assert fresh.thesis == "" and fresh.cycles == []


# ─────────────────────────── BRAIN cycle ────────────────────────────────────

class TestOversightBrain:
    def test_disabled_is_neutral_noop_but_writes_state(self, tmp_path):
        b = OversightBrain(run_dir=tmp_path)
        v = b.cycle(_SNAP)
        assert v == OversightVerdict()                         # neutral
        st = json.loads((tmp_path / "oversight_state.json").read_text())
        assert st["oversight_risk_flag"] == "normal" and st["oversight_veto_side"] == ""
        # scratchpad + cycle log written
        assert (tmp_path / "oversight_scratchpad_2026-06-05.json").exists()
        assert (tmp_path / "oversight_cycles_2026-06-05.jsonl").exists()

    def test_enabled_writes_veto_from_lean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAIN_OVERSIGHT_ENABLED", "true")
        monkeypatch.setenv("BRAIN_LLM_API_KEY", "k")
        # _SNAP is a +0.5% UP tape, so a CE (bullish) lean is consistent and
        # survives verification → it vetoes the opposite (PE) side.
        monkeypatch.setattr(
            brain_mod, "reason",
            lambda *a, **k: OversightVerdict(posture="trend_up", direction_lean="CE",
                                             lean_confidence=0.75, risk_flag="reduce",
                                             thesis="bullish: holding above PDH"),
        )
        b = OversightBrain(run_dir=tmp_path)
        v = b.cycle(_SNAP, prior_fii_cr=-8776, events=["US CPI 10 Jul"])
        assert v.direction_lean == "CE"
        st = json.loads((tmp_path / "oversight_state.json").read_text())
        assert st["oversight_veto_side"] == "PE"               # bullish lean ⇒ don't take PE
        assert st["oversight_risk_flag"] == "reduce"

    def test_enabled_kills_hallucinated_lean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRAIN_OVERSIGHT_ENABLED", "true")
        monkeypatch.setenv("BRAIN_LLM_API_KEY", "k")
        # bearish lean on the +0.5% up tape = hallucination → verified away → no veto
        monkeypatch.setattr(
            brain_mod, "reason",
            lambda *a, **k: OversightVerdict(direction_lean="PE", lean_confidence=0.9),
        )
        b = OversightBrain(run_dir=tmp_path)
        b.cycle(_SNAP)
        st = json.loads((tmp_path / "oversight_state.json").read_text())
        assert st["oversight_veto_side"] == ""                 # contradiction killed → no veto

    def test_read_risk_state(self, tmp_path):
        OversightBrain(run_dir=tmp_path).cycle(_SNAP)
        st = OversightBrain.read_risk_state(tmp_path)
        assert st["oversight_risk_flag"] == "normal"


# ─────────────────────────── engine-side gate ───────────────────────────────

from strategy_app.brain.oversight.gate import oversight_entry_veto


class TestGate:
    def test_stand_down_vetoes_any_side(self):
        st = {"oversight_risk_flag": "stand_down", "oversight_veto_side": ""}
        assert oversight_entry_veto("CE", st)[0] is True
        assert oversight_entry_veto("PE", st)[0] is True

    def test_veto_side_blocks_only_that_side(self):
        st = {"oversight_risk_flag": "reduce", "oversight_veto_side": "CE"}
        assert oversight_entry_veto("CE", st)[0] is True       # bearish lean ⇒ no CE
        assert oversight_entry_veto("PE", st) == (False, "")    # PE still allowed

    def test_normal_allows(self):
        assert oversight_entry_veto("CE", {"oversight_risk_flag": "normal", "oversight_veto_side": ""}) == (False, "")

    def test_robust_to_garbage(self):
        assert oversight_entry_veto("CE", None) == (False, "")
