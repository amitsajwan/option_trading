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
    # iv_pct=93 > OTM_IV_CEIL default 92 (percentile threshold, see §3.3 fix).
    # We also lift the hard-reject ceiling to 95 so the code reaches the tier-ceiling
    # check rather than the reject path (otherwise reject at 90 fires first).
    snap = FakeSnap(iv_percentile=93.0)
    with _enable(**{"SMART_STRIKE_IV_REJECT_PCTILE": "95"}):
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
    # iv_pct=90 > OTM4 percentile ceil 89, but <= OTM3 ceil 90 (equals, not strictly above)
    # so OTM3 passes. Tests the percentile-threshold ordering of the corrected ceilings.
    snap = FakeSnap(iv_percentile=89.5, timestamp=_Hour(10))
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

# ---------------------------------------------------------------------------
# MAX PREMIUM cap — no edge above the cap, skip trade entirely
# ---------------------------------------------------------------------------

def test_max_premium_picks_deepest_affordable():
    # OTM4=250 ≤ 600 and passes gates → take OTM4
    snap = FakeSnap(
        iv_percentile=20.0,
        timestamp=_Hour(10),
        _ltp_table={
            ("CE", 48000): 1200.0,
            ("CE", 48100): 900.0,
            ("CE", 48200): 700.0,
            ("CE", 48300): 400.0,
            ("CE", 48400): 250.0,
        },
    )
    with _enable(
        "SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED",
        **{"SMART_STRIKE_MAX_PREMIUM": "600"},
    ):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.mode == "otm_4"
    assert sel.strike == 48400


def test_soft_cap_all_over_budget_falls_back_to_deepest_passing_tier():
    # SOFT cap (opt-in): all strikes > 600 → take the best strike anyway.
    # Pass 2: deepest tier that passes gates (conf+OI+regime) ignoring premium.
    snap = FakeSnap(
        iv_percentile=20.0,
        timestamp=_Hour(10),
        _ltp_table={
            ("CE", 48000): 1200.0,
            ("CE", 48100): 1000.0,
            ("CE", 48200): 800.0,
            ("CE", 48300): 700.0,
            ("CE", 48400): 650.0,
        },
    )
    with _enable(
        "SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED",
        **{"SMART_STRIKE_MAX_PREMIUM": "600", "SMART_STRIKE_HARD_PREMIUM_CAP": "0"},
    ):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    # Soft cap → always get a strike, not None
    assert sel.strike is not None
    assert sel.otm_steps > 0   # deepest possible tier returned


def test_soft_cap_atm_fallback_always_returns_strike():
    # SOFT cap (opt-in): low conf → no OTM tier passes → return ATM even over budget.
    snap = FakeSnap(
        iv_percentile=30.0,
        _ltp_table={("CE", 48000): 1200.0, ("CE", 48100): 900.0},
    )
    with _enable(**{
        "SMART_STRIKE_MAX_PREMIUM": "600",
        "SMART_STRIKE_HARD_PREMIUM_CAP": "0",
        "SMART_STRIKE_OTM_CONFIDENCE": "0.90",
    }):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.40))
    assert sel.strike == 48000   # ATM — soft cap never skips
    assert sel.mode == "atm"


def test_hard_cap_is_default_over_budget_vetoes():
    # No SMART_STRIKE_HARD_PREMIUM_CAP set → hard cap is the DEFAULT.
    # ATM (1200) over the 600 budget, no OTM fits → veto (skip).
    snap = FakeSnap(
        iv_percentile=30.0,
        _ltp_table={("CE", 48000): 1200.0, ("CE", 48100): 900.0},
    )
    with _enable(**{
        "SMART_STRIKE_MAX_PREMIUM": "600",
        "SMART_STRIKE_OTM_CONFIDENCE": "0.90",
    }):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.40))
    assert sel.strike is None
    assert sel.mode == "rejected_premium_cap"


def test_hard_cap_all_over_budget_vetoes_trade():
    # Every strike > 600 AND hard cap on → no affordable strike → SKIP (None).
    snap = FakeSnap(
        iv_percentile=20.0,
        timestamp=_Hour(10),
        _ltp_table={
            ("CE", 48000): 1200.0,
            ("CE", 48100): 1000.0,
            ("CE", 48200): 800.0,
            ("CE", 48300): 700.0,
            ("CE", 48400): 650.0,
        },
    )
    with _enable(
        "SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED",
        **{"SMART_STRIKE_MAX_PREMIUM": "600", "SMART_STRIKE_HARD_PREMIUM_CAP": "1"},
    ):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.strike is None
    assert sel.mode == "rejected_premium_cap"


def test_hard_cap_affordable_atm_still_trades():
    # Low conf → no OTM passes, but ATM (500) is within the 600 budget → trade ATM.
    snap = FakeSnap(
        iv_percentile=30.0,
        _ltp_table={("CE", 48000): 500.0, ("CE", 48100): 900.0},
    )
    with _enable(**{
        "SMART_STRIKE_MAX_PREMIUM": "600",
        "SMART_STRIKE_HARD_PREMIUM_CAP": "1",
        "SMART_STRIKE_OTM_CONFIDENCE": "0.90",
    }):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.40))
    assert sel.strike == 48000
    assert sel.mode == "atm"


def test_hard_cap_over_budget_atm_vetoes():
    # ATM itself (1200) exceeds the 600 budget and no OTM fits → veto.
    snap = FakeSnap(
        iv_percentile=30.0,
        _ltp_table={("CE", 48000): 1200.0, ("CE", 48100): 900.0},
    )
    with _enable(**{
        "SMART_STRIKE_MAX_PREMIUM": "600",
        "SMART_STRIKE_HARD_PREMIUM_CAP": "1",
        "SMART_STRIKE_OTM_CONFIDENCE": "0.90",
    }):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.40))
    assert sel.strike is None
    assert sel.mode == "rejected_premium_cap"


def test_hard_cap_affordable_otm_still_picked():
    # Hard cap on but a cheap OTM exists within budget → Pass 1 still selects it.
    snap = FakeSnap(
        iv_percentile=20.0,
        timestamp=_Hour(10),
        _ltp_table={
            ("CE", 48000): 1200.0,
            ("CE", 48100): 900.0,
            ("CE", 48200): 700.0,
            ("CE", 48300): 400.0,
            ("CE", 48400): 250.0,
        },
    )
    with _enable(
        "SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED",
        **{"SMART_STRIKE_MAX_PREMIUM": "600", "SMART_STRIKE_HARD_PREMIUM_CAP": "1"},
    ):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.mode == "otm_4"
    assert sel.strike == 48400


def test_hard_cap_with_zero_premium_does_not_veto():
    # max_premium=0 means "no cap" — hard-cap flag must not veto when there is no budget.
    snap = FakeSnap(
        iv_percentile=30.0,
        _ltp_table={("CE", 48000): 1200.0},
    )
    with _enable(**{
        "SMART_STRIKE_MAX_PREMIUM": "0",
        "SMART_STRIKE_HARD_PREMIUM_CAP": "1",
        "SMART_STRIKE_OTM_CONFIDENCE": "0.90",
    }):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.40))
    assert sel.strike == 48000
    assert sel.mode == "atm"


def test_max_premium_zero_means_no_cap():
    snap = FakeSnap(iv_percentile=30.0)
    with _enable(**{"SMART_STRIKE_MAX_PREMIUM": "0"}):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.40))
    assert sel.mode == "atm"


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


# ---------------------------------------------------------------------------
# STRIKE-S4 — IV-ceiling percentile-not-absolute regression (§3.3 fix)
# ---------------------------------------------------------------------------
# On 2026-06-01, iv_percentile was at the 86th percentile. The old ceilings
# (OTM1=60, OTM2=50, OTM3=40, OTM4=30) looked like absolute IV but were compared
# against iv_percentile (0–100). 86 > all of 30–60 → every OTM tier rejected → ATM
# locked on every active day. Fixed to percentile thresholds (89–92).
# This test locks that fix in: the old ceilings reject OTM at iv_pct=86; the new
# default percentile ceilings accept it.

def test_iv_percentile_86_passes_new_percentile_ceilings():
    """iv_percentile=86 is below the corrected OTM1 ceil of 92 — tier should be reached."""
    snap = FakeSnap(iv_percentile=86.0)
    with _enable():  # uses corrected defaults: OTM_IV_CEIL=92
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.60))
    assert sel.mode == "otm_1", (
        f"Expected otm_1 at iv_pct=86 with percentile ceiling=92, got {sel.mode}. "
        "If this fails, IV ceilings have been reverted to absolute-IV values."
    )


# ---------------------------------------------------------------------------
# STRIKE-S1 — STRATEGY_STRIKE_MAX_OTM_STEPS honoured (was ignored, hard-capped at 4)
# ---------------------------------------------------------------------------

def test_max_otm_steps_limits_tier_count():
    """With MAX_OTM_STEPS=2, only tiers 1 and 2 are built even if 3+4 are enabled."""
    snap = FakeSnap(iv_percentile=20.0, timestamp=_Hour(10))
    with _enable(
        "SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED",
        **{"STRATEGY_STRIKE_MAX_OTM_STEPS": "2"},
    ):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.otm_steps <= 2, f"Expected OTM ≤ 2 steps, got {sel.otm_steps}"


def test_max_otm_steps_zero_or_one_gives_otm1():
    """MAX_OTM_STEPS=1 should still produce OTM-1 (minimum is 1)."""
    snap = FakeSnap(iv_percentile=20.0)
    with _enable(**{"STRATEGY_STRIKE_MAX_OTM_STEPS": "1"}):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.60))
    assert sel.otm_steps == 1


def test_max_otm_steps_8_allows_deeper_tiers_when_enabled():
    """MAX_OTM_STEPS=8 with OTM5 enabled should allow tier-5 selection."""
    snap = FakeSnap(
        iv_percentile=20.0,
        timestamp=_Hour(9),
        _ltp_table={
            ("CE", 48000): 1200.0,
            ("CE", 48100): 900.0,
            ("CE", 48200): 650.0,
            ("CE", 48300): 400.0,
            ("CE", 48400): 250.0,
            ("CE", 48500): 150.0,   # OTM-5
        },
    )
    with _enable(
        "SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED",
        "SMART_STRIKE_OTM4_ENABLED", "SMART_STRIKE_OTM5_ENABLED",
        **{
            "STRATEGY_STRIKE_MAX_OTM_STEPS": "8",
            "SMART_STRIKE_OTM5_CONFIDENCE": "0.90",   # meets ce_prob=0.90
            "SMART_STRIKE_OTM5_IV_CEIL": "92",
            "SMART_STRIKE_OTM5_REGIMES": "BREAKOUT",
            "SMART_STRIKE_OTM5_MAX_BAR_HOUR": "11",
            "SMART_STRIKE_OTM5_MIN_OI": "0",
        },
    ):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.otm_steps == 5, f"Expected OTM-5, got {sel.otm_steps} ({sel.mode})"


def test_iv_percentile_86_rejected_by_old_absolute_ceilings():
    """With the old absolute-looking ceilings (60/50/40/30), iv_pct=86 should reject all OTM."""
    snap = FakeSnap(iv_percentile=86.0)
    with _enable(
        "SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED",
        **{
            "SMART_STRIKE_OTM_IV_CEIL":  "60",  # old: treated as absolute IV → rejects at pct=86
            "SMART_STRIKE_OTM2_IV_CEIL": "50",
            "SMART_STRIKE_OTM3_IV_CEIL": "40",
            "SMART_STRIKE_OTM4_IV_CEIL": "30",
        },
    ):
        sel = select_strike(snap, "CE", FakeDecision(ce_prob=0.90), regime="BREAKOUT")
    assert sel.mode == "atm", (
        f"Expected ATM with old absolute-style ceilings at iv_pct=86, got {sel.mode}."
    )
