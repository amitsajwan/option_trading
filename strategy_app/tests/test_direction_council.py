"""Tests for the regime-conditioned confluence council (the trader checklist).

Verifies: range -> abstain; trend + confluence -> directional; trend but
insufficient agreement -> abstain; regime requires momentum to confirm VWAP.
No model (DIRECTION_ML_MODEL_PATH unset) -> members are vwap/max_pain/pcr.
"""
from __future__ import annotations

import pytest

from strategy_app.contracts import Direction
from strategy_app.engines.strategies.entry_direction_policy import _regime_council_direction


class _Snap:
    def __init__(self, pv, ret5, atm, max_pain, pcr_chg):
        self.raw_payload = {"futures_derived": {"price_vs_vwap": pv}}
        self.fut_return_5m = ret5
        self.atm_strike = atm
        self.max_pain = max_pain
        self.pcr_change_5m = pcr_chg


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("DIRECTION_ML_MODEL_PATH", "")        # no model member
    monkeypatch.setenv("DIR_COUNCIL_MIN_AGREE", "3")
    monkeypatch.setenv("DIR_REGIME_TREND_DIST", "0.0015")
    monkeypatch.setenv("DIR_MAXPAIN_MIN_PTS", "50")
    monkeypatch.setenv("DIR_PCR_MIN_CHG", "0.02")


def test_range_regime_abstains():
    # |price_vs_vwap| below trend distance -> range -> abstain (caller straddles)
    d, rs = _regime_council_direction(_Snap(0.0005, 0.001, 57000, 57300, 0.05), {})
    assert d is None
    assert rs["council_regime"] == "range"
    assert rs["council_result"] == "range_abstain"


def test_trend_needs_momentum_to_confirm_vwap():
    # price above vwap but 5m momentum DOWN -> not a trend -> abstain
    d, rs = _regime_council_direction(_Snap(0.003, -0.002, 57000, 57300, 0.05), {})
    assert d is None and rs["council_regime"] == "range"


def test_trend_up_with_full_confluence_takes_CE():
    # trend up + vwap(+) + max_pain magnet up (atm<mp) + pcr rising = 3 agree -> CE
    d, rs = _regime_council_direction(_Snap(0.003, 0.002, 57000, 57300, 0.05), {})
    assert d == Direction.CE
    assert rs["council_agree"] == 3 and rs["council_result"].startswith("confluence")


def test_trend_down_with_full_confluence_takes_PE():
    # trend down + vwap(-) + max_pain magnet down (atm>mp) + pcr falling = PE
    d, rs = _regime_council_direction(_Snap(-0.003, -0.002, 57300, 57000, -0.05), {})
    assert d == Direction.PE and rs["council_agree"] == 3


def test_trend_but_insufficient_confluence_abstains():
    # trend up, but max_pain within 50pt (skipped) and pcr flat (skipped) -> only vwap -> abstain
    d, rs = _regime_council_direction(_Snap(0.003, 0.002, 57000, 57010, 0.0), {})
    assert d is None
    assert rs["council_agree"] == 1 and "insufficient_confluence" in rs["council_result"]


def test_dissent_outvotes_when_min_agree_lowered(monkeypatch):
    # lower min_agree to 2: vwap(+) + pcr(+) agree, max_pain(-) dissents -> 2>=2 and 2>1 -> CE
    monkeypatch.setenv("DIR_COUNCIL_MIN_AGREE", "2")
    d, rs = _regime_council_direction(_Snap(0.003, 0.002, 57300, 57000, 0.05), {})
    assert d == Direction.CE
    assert rs["council_agree"] == 2 and rs["council_against"] == 1
