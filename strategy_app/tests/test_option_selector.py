"""Tests for option_selector — 4-tier smart strike selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import patch

from strategy_app.signals.option_selector import select_strike


@dataclass
class FakeSnap:
    atm_strike: Optional[int] = 48000
    _step: Optional[int] = 100
    iv_percentile: Optional[float] = 50.0
    _ltp_table: Optional[dict] = None   # {(dir, strike): ltp}
    _oi_table: Optional[dict] = None    # {(dir, strike): oi}
    timestamp: Optional[object] = None

    def strike_step(self) -> Optional[int]:
        return self._step

    def option_ltp(self, direction: str, strike: int) -> Optional[float]:
        if self._ltp_table is None:
            return 500.0  # default: every strike has a price
        return self._ltp_table.get((direction, strike))

    def option_oi(self, direction: str, strike: int) -> Optional[float]:
        if self._oi_table is None:
            return 200_000.0  # default: plenty of OI everywhere
        # Table overrides specific strikes; missing entries still get the good default
        return self._oi_table.get((direction, strike), 200_000.0)


@dataclass
class FakeDecision:
    ce_prob: float = 0.5
    pe_prob: float = 0.5


class _Hour:
    def __init__(self, h: int):
        self.hour = h


def _enable(*extra_vars: str, **kw):
    env = {"STRATEGY_SMART_STRIKE_ENABLED": "1"}
    for v in extra_vars:
        env[v] = "1"
    env.update(kw)
    return patch.dict("os.environ", env)


# ---------------------------------------------------------------------------
# Baseline / legacy
# ---------------------------------------------------------------------------

def test_disabled_returns_atm():
    snap = FakeSnap()
    dec = FakeDecision(ce_prob=0.95)
    import os; os.environ.pop("STRATEGY_SMART_STRIKE_ENABLED", None)
    with patch.dict("os.environ", {}, clear=False):
        sel = select_strike(snap, "CE", dec)
    assert sel.strike == 48000
    assert sel.mode == "legacy_atm"
    assert sel.otm_steps == 0


def test_high_iv_rejects_trade():
    snap = FakeSnap(iv_percentile=95.0)
    dec = FakeDecision(ce_prob=0.9)
    with _enable():
        sel = select_strike(snap, "CE", dec)
    assert sel.strike is None
    assert sel.mode == "rejected_high_iv"


def test_missing_atm_returns_none():
    snap = FakeSnap(atm_strike=None)
    with _enable():
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.9))
    assert sel.strike is None


def test_no_strike_step_returns_atm():
    snap = FakeSnap(_step=None)
    with _enable():
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.9))
    assert sel.strike == 48000
    assert sel.mode == "atm"


def test_low_confidence_returns_atm():
    # Default OTM1 gate is 0.55; conf 0.40 stays ATM
    snap = FakeSnap(iv_percentile=30.0)
    with _enable():
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.40))
    assert sel.strike == 48000
    assert sel.mode == "atm"
    assert sel.otm_steps == 0


def test_otm1_missing_ltp_falls_back_to_atm():
    snap = FakeSnap(
        iv_percentile=30.0,
        _ltp_table={("CE", 48000): 500.0},  # only ATM has LTP
    )
    with _enable():
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.80))
    assert sel.strike == 48000
    assert sel.mode == "atm"


# ---------------------------------------------------------------------------
# Tier 1 (1-OTM, conf >= 0.55 default)
# ---------------------------------------------------------------------------

def test_tier1_ce_picks_otm1():
    snap = FakeSnap(iv_percentile=30.0)
    with _enable():
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.60))
    assert sel.strike == 48100   # ATM + 1 step
    assert sel.mode == "otm_1"
    assert sel.otm_steps == 1


def test_tier1_pe_picks_otm1():
    snap = FakeSnap(iv_percentile=30.0)
    with _enable():
        sel = select_strike(snap, "PE", FakeDecision(pe_prob=0.60))
    assert sel.strike == 47900   # ATM - 1 step
    assert sel.mode == "otm_1"


def test_tier1_iv_too_high_returns_atm():
    # iv_pct=65 > OTM_IV_CEIL default 60
    snap = FakeSnap(iv_percentile=65.0)
    with _enable():
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.80))
    assert sel.mode == "atm"


def test_tier1_conf_override_via_env():
    snap = FakeSnap(iv_percentile=30.0)
    with _enable(**{"SMART_STRIKE_OTM_CONFIDENCE": "0.90"}):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.80))
    assert sel.mode == "atm"   # 0.80 < 0.90 override


# ---------------------------------------------------------------------------
# Tier 2 (2-OTM, conf >= 0.65 default)
# ---------------------------------------------------------------------------

def test_tier2_picks_otm2_when_conf_meets():
    snap = FakeSnap(iv_percentile=30.0)
    with _enable("SMART_STRIKE_OTM2_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.70))
    assert sel.strike == 48200
    assert sel.mode == "otm_2"
    assert sel.otm_steps == 2


def test_tier2_falls_back_to_otm1_when_conf_low():
    snap = FakeSnap(iv_percentile=30.0)
    with _enable("SMART_STRIKE_OTM2_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.60))
    assert sel.mode == "otm_1"


def test_tier2_oi_gate_blocks():
    snap = FakeSnap(
        iv_percentile=30.0,
        _oi_table={("CE", 48200): 50_000.0},  # below 100k default
    )
    with _enable("SMART_STRIKE_OTM2_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.70))
    assert sel.mode == "otm_1"   # OI too low → fall back


def test_tier2_regime_gate():
    snap = FakeSnap(iv_percentile=30.0)
    with _enable("SMART_STRIKE_OTM2_ENABLED", **{"SMART_STRIKE_OTM2_REGIMES": "BREAKOUT"}):
        sel_b = select_strike(snap, "CE", FakeDecision(ce_prob=0.70), regime="BREAKOUT")
        sel_s = select_strike(snap, "CE", FakeDecision(ce_prob=0.70), regime="SIDEWAYS")
    assert sel_b.mode == "otm_2"
    assert sel_s.mode == "otm_1"


# ---------------------------------------------------------------------------
# Tier 3 (3-OTM, conf >= 0.75 default, BREAKOUT/TRENDING)
# ---------------------------------------------------------------------------

def test_tier3_picks_otm3_breakout():
    snap = FakeSnap(iv_percentile=25.0, timestamp=_Hour(10))
    with _enable("SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.80), regime="BREAKOUT")
    assert sel.strike == 48300
    assert sel.mode == "otm_3"
    assert sel.otm_steps == 3


def test_tier3_falls_back_to_otm2_when_regime_wrong():
    snap = FakeSnap(iv_percentile=25.0)
    with _enable("SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.80), regime="SIDEWAYS")
    assert sel.mode == "otm_2"


def test_tier3_hour_gate():
    snap = FakeSnap(iv_percentile=25.0, timestamp=_Hour(13))  # past 12:00
    with _enable("SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.80), regime="BREAKOUT")
    assert sel.mode == "otm_2"


# ---------------------------------------------------------------------------
# Tier 4 (4-OTM, conf >= 0.85 default, BREAKOUT only, before 11:00)
# ---------------------------------------------------------------------------

def test_tier4_picks_otm4_perfect_conditions():
    snap = FakeSnap(iv_percentile=20.0, timestamp=_Hour(10))
    with _enable("SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.strike == 48400
    assert sel.mode == "otm_4"
    assert sel.otm_steps == 4


def test_tier4_falls_back_when_iv_too_high():
    # IV=35 > OTM4 ceil 30, passes OTM3 ceil 40
    snap = FakeSnap(iv_percentile=35.0, timestamp=_Hour(10))
    with _enable("SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.mode == "otm_3"


def test_tier4_falls_back_when_late_session():
    snap = FakeSnap(iv_percentile=20.0, timestamp=_Hour(11))  # not < 11
    with _enable("SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.mode == "otm_3"


def test_tier4_oi_gate_blocks_falls_back_to_otm3():
    snap = FakeSnap(
        iv_percentile=20.0,
        timestamp=_Hour(10),
        _oi_table={("CE", 48400): 10_000.0},   # OTM4 OI too low
    )
    with _enable("SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.mode == "otm_3"


# ---------------------------------------------------------------------------
# Missing LTP at deep tier cascades to shallower
# ---------------------------------------------------------------------------

def test_missing_ltp_at_otm4_falls_back_to_otm3():
    snap = FakeSnap(
        iv_percentile=20.0,
        timestamp=_Hour(10),
        _ltp_table={
            ("CE", 48000): 1200.0,
            ("CE", 48100): 900.0,
            ("CE", 48200): 600.0,
            ("CE", 48300): 350.0,
            ("CE", 48400): None,   # OTM4 has no LTP
        },
    )
    with _enable("SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED"):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.mode == "otm_3"
    assert sel.strike == 48300


# ---------------------------------------------------------------------------
# Env var overrides propagate correctly
# ---------------------------------------------------------------------------

def test_iv_reject_threshold_overridable():
    snap = FakeSnap(iv_percentile=60.0)
    with _enable(**{"SMART_STRIKE_IV_REJECT_PCTILE": "55.0"}):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.9))
    assert sel.mode == "rejected_high_iv"


def test_otm3_custom_regime_list():
    snap = FakeSnap(iv_percentile=25.0, timestamp=_Hour(10))
    with _enable("SMART_STRIKE_OTM3_ENABLED", **{"SMART_STRIKE_OTM3_REGIMES": "SIDEWAYS,BREAKOUT"}):
        sel_s = select_strike(snap, "CE", FakeDecision(ce_prob=0.80), regime="SIDEWAYS")
        sel_t = select_strike(snap, "CE", FakeDecision(ce_prob=0.80), regime="TRENDING")
    assert sel_s.mode == "otm_3"
    assert sel_t.mode == "otm_1"   # TRENDING not in custom list
