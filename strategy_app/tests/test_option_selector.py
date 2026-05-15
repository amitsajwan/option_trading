"""Tests for option_selector (Phase 1.3 smart strike selection)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

import pytest

from strategy_app.engines.option_selector import select_strike


@dataclass
class FakeSnap:
    # iv_percentile is on 0-100 scale (matches snapshot.iv_derived.iv_percentile)
    atm_strike: Optional[int] = 48000
    _step: Optional[int] = 100
    iv_percentile: Optional[float] = 50.0
    _ltp_table: dict = None

    def strike_step(self) -> Optional[int]:
        return self._step

    def option_ltp(self, direction: str, strike: int) -> Optional[float]:
        if self._ltp_table is None:
            return 100.0
        return self._ltp_table.get((direction, strike))


@dataclass
class FakeDecision:
    ce_prob: float = 0.5
    pe_prob: float = 0.5
    action: str = "BUY_CE"


def _enable():
    return patch.dict("os.environ", {"STRATEGY_SMART_STRIKE_ENABLED": "1"})


def test_disabled_returns_atm_unchanged():
    snap = FakeSnap()
    dec = FakeDecision(ce_prob=0.95)
    with patch.dict("os.environ", {}, clear=False):
        # Make sure flag is off
        import os
        os.environ.pop("STRATEGY_SMART_STRIKE_ENABLED", None)
        sel = select_strike(snap, "CE", dec)
    assert sel.strike == 48000
    assert sel.mode == "legacy_atm"


def test_high_iv_rejects_trade():
    snap = FakeSnap(iv_percentile=95.0)
    dec = FakeDecision(ce_prob=0.9)
    with _enable():
        sel = select_strike(snap, "CE", dec)
    assert sel.strike is None
    assert sel.mode == "rejected_high_iv"


def test_high_confidence_low_iv_picks_otm_ce():
    snap = FakeSnap(iv_percentile=30.0)
    dec = FakeDecision(ce_prob=0.85)
    with _enable():
        sel = select_strike(snap, "CE", dec)
    assert sel.strike == 48100  # ATM + step for CE
    assert sel.mode == "otm_1"


def test_high_confidence_low_iv_picks_otm_pe():
    snap = FakeSnap(iv_percentile=30.0)
    dec = FakeDecision(pe_prob=0.85)
    with _enable():
        sel = select_strike(snap, "PE", dec)
    assert sel.strike == 47900  # ATM - step for PE
    assert sel.mode == "otm_1"


def test_high_confidence_high_iv_falls_back_to_atm():
    # IV > OTM_IV_CEIL (50) but < IV_REJECT (90) → ATM, not OTM, not rejected
    snap = FakeSnap(iv_percentile=70.0)
    dec = FakeDecision(ce_prob=0.85)
    with _enable():
        sel = select_strike(snap, "CE", dec)
    assert sel.strike == 48000
    assert sel.mode == "atm"


def test_low_confidence_picks_atm():
    snap = FakeSnap(iv_percentile=30.0)
    dec = FakeDecision(ce_prob=0.55)
    with _enable():
        sel = select_strike(snap, "CE", dec)
    assert sel.strike == 48000
    assert sel.mode == "atm"


def test_otm_missing_premium_falls_back_to_atm():
    # OTM strike has no LTP → must not return None; falls back to ATM
    snap = FakeSnap(
        iv_percentile=30.0,
        _ltp_table={("CE", 48000): 50.0, ("CE", 48100): None},
    )
    dec = FakeDecision(ce_prob=0.85)
    with _enable():
        sel = select_strike(snap, "CE", dec)
    assert sel.strike == 48000
    assert sel.mode == "atm"
    assert sel.reason == "atm_otm_missing_premium"


def test_no_strike_step_falls_back_to_atm():
    snap = FakeSnap(_step=None, iv_percentile=30.0)
    dec = FakeDecision(ce_prob=0.85)
    with _enable():
        sel = select_strike(snap, "CE", dec)
    assert sel.strike == 48000
    assert sel.mode == "atm"


def test_missing_atm_returns_none():
    snap = FakeSnap(atm_strike=None)
    dec = FakeDecision(ce_prob=0.85)
    with _enable():
        sel = select_strike(snap, "CE", dec)
    assert sel.strike is None
    assert sel.reason == "missing_atm_strike"


def test_iv_thresholds_overridable_via_env():
    # Lower IV reject threshold so 60 now rejects
    snap = FakeSnap(iv_percentile=60.0)
    dec = FakeDecision(ce_prob=0.85)
    with patch.dict("os.environ", {
        "STRATEGY_SMART_STRIKE_ENABLED": "1",
        "SMART_STRIKE_IV_REJECT_PCTILE": "55.0",
    }):
        sel = select_strike(snap, "CE", dec)
    assert sel.strike is None
    assert sel.mode == "rejected_high_iv"


def test_confidence_threshold_overridable_via_env():
    # Raise OTM confidence threshold above 0.85 so we fall back to ATM
    snap = FakeSnap(iv_percentile=30.0)
    dec = FakeDecision(ce_prob=0.85)
    with patch.dict("os.environ", {
        "STRATEGY_SMART_STRIKE_ENABLED": "1",
        "SMART_STRIKE_OTM_CONFIDENCE": "0.90",
    }):
        sel = select_strike(snap, "CE", dec)
    assert sel.strike == 48000
    assert sel.mode == "atm"
