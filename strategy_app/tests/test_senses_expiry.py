"""Tests for the Expiry sense (DTE-aware moneyness preference)."""
from __future__ import annotations

from strategy_app.senses.expiry import ExpirySense


def test_abstains_without_dte():
    assert ExpirySense().evaluate({}).is_abstain


def test_far_dte_prefers_otm():
    v = ExpirySense().evaluate({"days_to_expiry": 20})
    assert v.verdict == "far" and v.evidence["preferred_moneyness"] == "OTM"


def test_near_dte_prefers_atm():
    assert ExpirySense().evaluate({"days_to_expiry": 2}).evidence["preferred_moneyness"] == "ATM"
    assert ExpirySense().evaluate({"days_to_expiry": 1}).verdict == "expiry_day"


def test_expensive_atm_forces_otm_even_near_expiry():
    # near expiry would prefer ATM, but an unaffordable ATM premium overrides -> OTM
    v = ExpirySense().evaluate({"days_to_expiry": 2, "atm_premium": 1500.0, "affordable_premium": 1300.0})
    assert v.evidence["atm_expensive"] is True
    assert v.evidence["preferred_moneyness"] == "OTM"


def test_affordable_atm_keeps_atm_near_expiry():
    v = ExpirySense().evaluate({"days_to_expiry": 2, "atm_premium": 200.0, "affordable_premium": 1300.0})
    assert v.evidence["atm_expensive"] is False and v.evidence["preferred_moneyness"] == "ATM"
