"""Unit tests for RegimeDirector detectors and DualEntryConfirmer guards."""
from types import SimpleNamespace

from strategy_app.brain.regime_director import RegimeDirector, CE, PE, ABSTAIN
from strategy_app.ml.dual_entry_confirmer import DualEntryConfirmer


def snap(**kw):
    base = dict(
        fut_return_15m=None, ema_9=None, ema_21=None, price_vs_vwap=None,
        vwap=None, fut_close=None, atm_ce_oi_change_30m=None,
        atm_pe_oi_change_30m=None, max_pain=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# CE-agreeing snapshot: mom15 up, ce-OI < pe-OI (=> CE), spot below max_pain (=> CE)
CE_AGREE = dict(
    fut_return_15m=0.002, atm_ce_oi_change_30m=100.0, atm_pe_oi_change_30m=500.0,
    fut_close=100.0, max_pain=200, ema_9=110.0, ema_21=100.0, price_vs_vwap=0.001,
)
PE_AGREE = dict(
    fut_return_15m=-0.002, atm_ce_oi_change_30m=500.0, atm_pe_oi_change_30m=100.0,
    fut_close=200.0, max_pain=100, ema_9=100.0, ema_21=110.0, price_vs_vwap=-0.001,
)


def test_agreement_lever_fires_on_full_agreement():
    assert RegimeDirector("agreement_lever").decide(snap(**CE_AGREE)).side == CE
    assert RegimeDirector("agreement_lever").decide(snap(**PE_AGREE)).side == PE


def test_agreement_lever_abstains_on_disagreement():
    s = snap(**{**CE_AGREE, "max_pain": 50})  # spot 100 > max_pain 50 -> PE, breaks trio
    v = RegimeDirector("agreement_lever").decide(s)
    assert v.side == ABSTAIN
    assert v.breakdown["max_pain"] == PE and v.breakdown["mom15"] == CE


def test_ema_cross():
    assert RegimeDirector("ema_cross").decide(snap(ema_9=110.0, ema_21=100.0)).side == CE
    assert RegimeDirector("ema_cross").decide(snap(ema_9=100.0, ema_21=110.0)).side == PE
    assert RegimeDirector("ema_cross").decide(snap()).side == ABSTAIN  # missing -> abstain


def test_vwap_and_fade_are_opposite():
    s = snap(price_vs_vwap=0.001)
    assert RegimeDirector("vwap").decide(s).side == CE
    assert RegimeDirector("fade_vwap").decide(s).side == PE


def test_combo_requires_lever_and_ema_agree():
    assert RegimeDirector("combo").decide(snap(**CE_AGREE)).side == CE  # both CE
    s = snap(**{**CE_AGREE, "ema_9": 100.0, "ema_21": 110.0})  # ema says PE, lever CE
    assert RegimeDirector("combo").decide(s).side == ABSTAIN


def test_unknown_signal_falls_back_to_default():
    d = RegimeDirector("does_not_exist")
    assert d.signal == "agreement_lever"


def test_missing_fields_never_raise():
    assert RegimeDirector("agreement_lever").decide(snap()).side == ABSTAIN


class _Bias:
    def __init__(self, side, conviction, grounded, news="news"):
        self.side, self.conviction, self.grounded, self.news_summary = side, conviction, grounded, news


def test_llm_overlay_vetoes_contradicting_side():
    # structure says CE, grounded high-conviction LLM says PE -> ABSTAIN (veto)
    v = RegimeDirector("combo").decide(snap(**CE_AGREE), session_bias=_Bias("PE", 0.8, True))
    assert v.side == ABSTAIN and "VETO" in v.reason


def test_llm_overlay_agrees_boosts_and_annotates():
    v = RegimeDirector("combo").decide(snap(**CE_AGREE), session_bias=_Bias("CE", 0.8, True))
    assert v.side == CE and "AGREES" in v.reason


def test_llm_overlay_ignored_when_ungrounded_or_lowconv():
    base = RegimeDirector("combo").decide(snap(**CE_AGREE))
    v1 = RegimeDirector("combo").decide(snap(**CE_AGREE), session_bias=_Bias("PE", 0.9, False))  # ungrounded
    v2 = RegimeDirector("combo").decide(snap(**CE_AGREE), session_bias=_Bias("PE", 0.2, True))   # low conv
    assert v1.side == base.side == CE and v2.side == CE  # structure stands


def test_weighted_fires_on_clear_lean():
    v = RegimeDirector("weighted").decide(snap(**CE_AGREE))
    assert v.side == CE and v.confidence > 0.5


def test_weighted_abstains_on_split():
    # mom CE vs vwap PE, nothing else -> votes cancel -> abstain
    s = snap(fut_return_15m=0.002, price_vs_vwap=-0.001)
    assert RegimeDirector("weighted").decide(s).side == ABSTAIN


def test_weighted_graceful_when_oi_missing():
    # CE lean on every signal except OI (missing) -> still fires CE (no abstain on missing)
    s = snap(**{**CE_AGREE, "atm_ce_oi_change_30m": None, "atm_pe_oi_change_30m": None})
    assert RegimeDirector("weighted").decide(s).side == CE


def test_confirmer_side_validation_and_missing_bundle():
    c = DualEntryConfirmer(ce_path="", pe_path="")
    assert c.confirm("XX", snap()).fire is False           # bad side
    v = c.confirm("CE", snap())
    assert v.fire is False and v.model_loaded is False      # no bundle -> no fire
    rs = v.as_raw_signals()
    assert rs["dual_confirm_side"] == "CE" and rs["dual_confirm_fire"] is False
