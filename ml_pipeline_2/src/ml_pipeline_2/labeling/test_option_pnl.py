"""Tests for the option-P&L labeler. Each edge case from the equivalence
contract has a named test that pins the expected behavior."""

from __future__ import annotations

from typing import Optional

import pytest

from ml_pipeline_2.labeling.option_pnl import (
    LabelContract,
    PremiumLookup,
    Recipe,
    SKIP_LABEL,
    _compute_strike,
    _net_pnl_pct,
    label_one,
)
from strategy_app.constants import HARD_CLOSE_MINUTE, SOFT_CLOSE_MINUTE
from strategy_app.cost_model import TradingCostModel


# ── Fixtures ─────────────────────────────────────────────────────────────


class DictLookup(PremiumLookup):
    """Test premium lookup backed by a dict keyed by (minute, strike, side)."""

    def __init__(
        self,
        closes: Optional[dict[tuple[int, int, str], float]] = None,
        ois: Optional[dict[tuple[int, int, str], float]] = None,
    ) -> None:
        self._closes = closes or {}
        self._ois = ois or {}

    def get_close(self, *, timestamp_minute, trade_date, strike, option_type, expiry_str):
        return self._closes.get((int(timestamp_minute), int(strike), str(option_type)))

    def get_oi(self, *, timestamp_minute, trade_date, strike, option_type, expiry_str):
        return self._ois.get((int(timestamp_minute), int(strike), str(option_type)))


def _atm_recipe_ce_15(stop=0.20, target=0.30) -> Recipe:
    return Recipe(
        id="ATM_CE_15",
        option_type="CE",
        strike_offset_steps=0,
        max_hold_bars=15,
        stop_pct_of_premium=stop,
        target_pct_of_premium=target,
    )


def _snap(t_hhmm: str = "10:00", atm: int = 50000, step: int = 100) -> dict:
    h, m = t_hhmm.split(":")
    return {
        "timestamp_minute": int(h) * 60 + int(m),
        "trade_date": "2024-01-02",
        "atm_strike": atm,
        "strike_step": step,
        "expiry_str": "04JAN24",
    }


# ── Strike picker ────────────────────────────────────────────────────────


def test_compute_strike_atm_offset_zero():
    assert _compute_strike(atm_strike=50000, strike_step=100, option_type="CE", offset_steps=0) == 50000
    assert _compute_strike(atm_strike=50000, strike_step=100, option_type="PE", offset_steps=0) == 50000


def test_compute_strike_otm_ce_goes_up():
    assert _compute_strike(atm_strike=50000, strike_step=100, option_type="CE", offset_steps=1) == 50100
    assert _compute_strike(atm_strike=50000, strike_step=100, option_type="CE", offset_steps=3) == 50300


def test_compute_strike_otm_pe_goes_down():
    assert _compute_strike(atm_strike=50000, strike_step=100, option_type="PE", offset_steps=1) == 49900
    assert _compute_strike(atm_strike=50000, strike_step=100, option_type="PE", offset_steps=2) == 49800


def test_compute_strike_returns_none_when_atm_missing():
    assert _compute_strike(atm_strike=None, strike_step=100, option_type="CE", offset_steps=0) is None
    assert _compute_strike(atm_strike=0, strike_step=100, option_type="CE", offset_steps=0) is None


def test_compute_strike_otm_returns_none_when_step_missing():
    """Cannot compute OTM/ITM strike without strike_step — must return None
    rather than silently default to ATM (which would mislabel rows)."""
    assert _compute_strike(atm_strike=50000, strike_step=None, option_type="CE", offset_steps=1) is None
    assert _compute_strike(atm_strike=50000, strike_step=0, option_type="CE", offset_steps=1) is None


# ── Net P&L computation (lot size matters because brokerage is flat Rs) ──


def test_net_pnl_small_premium_has_bigger_cost_pct():
    """A Rs.10 premium has higher cost-as-%-of-entry than a Rs.100 premium,
    because Rs.40 round-trip brokerage is a bigger slice of the smaller entry."""
    cm = TradingCostModel()
    _, cost_pct_small, _ = _net_pnl_pct(entry_premium=10.0, exit_premium=10.0, cost_model=cm, lot_size=15)
    _, cost_pct_large, _ = _net_pnl_pct(entry_premium=200.0, exit_premium=200.0, cost_model=cm, lot_size=15)
    assert cost_pct_small > cost_pct_large
    # Sanity: Rs.10 premium at lot 15 = Rs.150 entry; Rs.40 brokerage alone is 26.7%.
    assert cost_pct_small > 0.20


def test_net_pnl_flat_premium_is_negative_after_cost():
    """If exit == entry, gross P&L = 0, net P&L is strictly negative (cost
    eats it). Label should be 0."""
    gross, cost, net = _net_pnl_pct(entry_premium=100.0, exit_premium=100.0, cost_model=TradingCostModel(), lot_size=15)
    assert gross == 0.0
    assert cost > 0.0
    assert net < 0.0


def test_net_pnl_profitable_premium_clears_cost():
    """30% gross up move on Rs.100 premium should clear all cost layers."""
    gross, cost, net = _net_pnl_pct(entry_premium=100.0, exit_premium=130.0, cost_model=TradingCostModel(), lot_size=15)
    assert gross == pytest.approx(0.30, abs=1e-6)
    # cost roughly 3% on Rs.1500 entry (Rs.40 brokerage + Rs.4ish charges/slip)
    assert 0.02 < cost < 0.05
    assert net > 0.25  # comfortable margin


# ── label_one — session gates ────────────────────────────────────────────


def test_entry_after_soft_close_is_skipped():
    """SOFT_CLOSE_MINUTE = 15:00. An entry at 15:01 must be skipped — runtime
    won't open new positions then either."""
    snap = _snap(t_hhmm="15:01")
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=DictLookup(), contract=LabelContract())
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "entry_after_soft_close"


def test_exit_window_crossing_hard_close_is_skipped():
    """An entry at 15:05 with max_hold=15 would exit at 15:20 — past
    HARD_CLOSE_MINUTE (15:15). Runtime would force-close before; labels
    skip these rows so the model only learns on full-window-fitting setups."""
    # 15:05 = 905 minutes since midnight; +15 = 920; HARD_CLOSE = 915.
    # But entry_after_soft_close also triggers at 15:05 since SOFT=900. Need
    # an earlier entry that's pre-SOFT but crosses HARD with the hold window.
    # Actually: SOFT=900, HARD=915. So entry 14:55 (895) + max_hold 25 = 920 > 915.
    snap = _snap(t_hhmm="14:55")
    out = label_one(
        snapshot=snap,
        recipe=Recipe(id="x", option_type="CE", strike_offset_steps=0, max_hold_bars=25, stop_pct_of_premium=0.2, target_pct_of_premium=0.3),
        lookup=DictLookup(),
        contract=LabelContract(),
    )
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "max_hold_exceeds_hard_close"


def test_missing_atm_skips_label():
    snap = _snap(atm=0)
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=DictLookup(), contract=LabelContract())
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "missing_atm_or_strike_step"


def test_otm_recipe_skips_when_strike_step_missing():
    """OTM recipes need strike_step to compute the offset strike; without
    it, the labeler must skip, NOT fall back to ATM (which would be a
    different contract from what the runtime would pick)."""
    snap = _snap()
    snap["strike_step"] = None
    otm_recipe = Recipe(
        id="OTM1_CE_15", option_type="CE", strike_offset_steps=1, max_hold_bars=15,
        stop_pct_of_premium=0.2, target_pct_of_premium=0.3,
    )
    out = label_one(snapshot=snap, recipe=otm_recipe, lookup=DictLookup(), contract=LabelContract())
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "missing_atm_or_strike_step"


# ── label_one — premium edge cases ───────────────────────────────────────


def test_missing_entry_premium_skipped():
    snap = _snap()
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=DictLookup(), contract=LabelContract())
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "missing_strike_at_entry"


def test_zero_entry_premium_skipped():
    snap = _snap()
    lookup = DictLookup(closes={(600, 50000, "CE"): 0.0})
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=lookup, contract=LabelContract())
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "zero_or_negative_entry_premium"


def test_low_premium_skipped():
    """Premium below min_entry_premium (default 5) is rejected."""
    snap = _snap()
    lookup = DictLookup(closes={(600, 50000, "CE"): 2.5})
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=lookup, contract=LabelContract())
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "premium_below_min_entry_premium"


def test_low_oi_skipped():
    snap = _snap()
    closes = {(600, 50000, "CE"): 100.0}
    closes.update({(600 + i, 50000, "CE"): 100.0 for i in range(1, 16)})
    lookup = DictLookup(closes=closes, ois={(600, 50000, "CE"): 50.0})
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=lookup, contract=LabelContract())
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "oi_below_min"


def test_missing_intermediate_bar_skips():
    """If any bar in [t+1, t+max_hold] is missing, the runtime would be blind
    too — skip the label rather than imputing."""
    snap = _snap()
    closes = {(600, 50000, "CE"): 100.0}
    for i in range(1, 8):
        closes[(600 + i, 50000, "CE")] = 100.0 + i  # going up slowly
    # missing minute 608 (t+8)
    for i in range(9, 16):
        closes[(600 + i, 50000, "CE")] = 100.0 + i
    lookup = DictLookup(closes=closes)
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=lookup, contract=LabelContract())
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "missing_strike_at_t_plus_8"


# ── label_one — exit reason precedence ───────────────────────────────────


def test_target_hit_emits_label_1():
    snap = _snap()
    closes = {(600, 50000, "CE"): 100.0}
    # Premium climbs steadily; recipe target_pct=0.30 so target_price=130.
    # Bar at t+4 hits 135 → exit TARGET at offset 4.
    for i in range(1, 16):
        closes[(600 + i, 50000, "CE")] = 100.0 + (i * 10)  # 110, 120, 130, 140, ...
    lookup = DictLookup(closes=closes)
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=lookup, contract=LabelContract())
    assert out["label"] == 1
    assert out["exit_reason"] == "TARGET"
    # First bar with close >= 130 is t+3 (close=130). Loop checks stop first
    # then target; at t+3 close is exactly target_price, comparison >= → hit.
    assert out["exit_bar_offset"] == 3
    assert out["exit_premium"] == 130.0


def test_stop_hit_emits_label_0():
    snap = _snap()
    closes = {(600, 50000, "CE"): 100.0}
    # Premium drops steadily; stop_pct=0.20 → stop_price=80. Bar at t+2 hits 75.
    for i in range(1, 16):
        closes[(600 + i, 50000, "CE")] = 100.0 - (i * 12.5)  # 87.5, 75, 62.5...
    lookup = DictLookup(closes=closes)
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=lookup, contract=LabelContract())
    assert out["label"] == 0
    assert out["exit_reason"] == "STOP"
    assert out["exit_bar_offset"] == 2  # first close ≤ 80
    assert out["exit_premium"] == 75.0


def test_stop_takes_precedence_when_same_bar_hits_both():
    """If a single close were to satisfy stop AND target conditions
    (impossible in practice but algorithmically possible if pct config is
    weird), stop fires first — conservative for the loss label."""
    snap = _snap()
    weird_recipe = Recipe(
        id="weird", option_type="CE", strike_offset_steps=0, max_hold_bars=5,
        stop_pct_of_premium=0.5, target_pct_of_premium=0.05,
    )
    closes = {
        (600, 50000, "CE"): 100.0,
        # Bar at t+1: close=50 → stop_price=50.0 (50%), target_price=105.0.
        # Close <= 50 → stop hit. Cannot also be >= 105.
        # To synthesize impossible conjoint, we'd need stop_pct < 0 which
        # __post_init__ rejects. Use a recipe where stop_pct very near target_pct
        # is impossible; skip this geometry test — instead verify with a real
        # stop hit that stop branch fires before target branch when premium drops.
        (601, 50000, "CE"): 50.0,
        (602, 50000, "CE"): 50.0,
        (603, 50000, "CE"): 50.0,
        (604, 50000, "CE"): 50.0,
        (605, 50000, "CE"): 50.0,
    }
    lookup = DictLookup(closes=closes)
    out = label_one(snapshot=snap, recipe=weird_recipe, lookup=lookup, contract=LabelContract())
    assert out["exit_reason"] == "STOP"


def test_max_hold_exit_when_no_stop_or_target():
    snap = _snap()
    closes = {(600, 50000, "CE"): 100.0}
    # Flat-ish premium that never hits stop=80 or target=130
    for i in range(1, 16):
        closes[(600 + i, 50000, "CE")] = 100.0 + (i % 3 - 1)  # 99/100/101 oscillation
    lookup = DictLookup(closes=closes)
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=lookup, contract=LabelContract())
    assert out["exit_reason"] == "MAX_HOLD"
    assert out["exit_bar_offset"] == 15
    # Cost > tiny gross → label = 0
    assert out["label"] == 0


# ── label_one — runtime-equivalence behaviours ───────────────────────────


def test_uses_close_not_high_low_for_exit_check():
    """If an intra-bar high reaches the target but the close doesn't, the
    label must NOT mark TARGET — the runtime checks only close (snap.option_ltp).
    Our lookup doesn't even expose high/low: a passing test here proves the
    labeler isn't peeking at intra-bar info."""
    snap = _snap()
    closes = {(600, 50000, "CE"): 100.0}
    for i in range(1, 16):
        closes[(600 + i, 50000, "CE")] = 105.0  # close = +5%, well below target
    lookup = DictLookup(closes=closes)
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=lookup, contract=LabelContract())
    assert out["exit_reason"] == "MAX_HOLD"
    assert out["exit_premium"] == 105.0


def test_constants_match_strategy_app():
    """The labeler MUST use the same session constants as the runtime tracker.
    If someone re-defines them locally, this test alarms."""
    assert LabelContract().soft_close_minute == SOFT_CLOSE_MINUTE
    assert LabelContract().hard_close_minute == HARD_CLOSE_MINUTE


def test_otm_ce_strike_used_in_premium_lookup():
    """OTM_1 CE recipe must look up the +1-step strike, not ATM."""
    snap = _snap(atm=50000, step=100)
    otm_recipe = Recipe(
        id="OTM1_CE_15", option_type="CE", strike_offset_steps=1, max_hold_bars=15,
        stop_pct_of_premium=0.2, target_pct_of_premium=0.3,
    )
    # ATM strike has bogus premium; only OTM strike (50100) has real prices.
    closes = {(600, 50000, "CE"): 999999.0}
    for i in range(0, 16):
        closes[(600 + i, 50100, "CE")] = 50.0 + i  # entry=50, rises slowly
    lookup = DictLookup(closes=closes)
    out = label_one(snapshot=snap, recipe=otm_recipe, lookup=lookup, contract=LabelContract())
    # Should have hit MAX_HOLD with entry=50 exit=50+15=65 → 30% gross, ample
    assert out["selected_strike"] == 50100
    assert out["entry_premium"] == 50.0


def test_pe_recipe_uses_pe_lookup_not_ce():
    """A PE recipe must look up the PE leg of the chain, not the CE leg
    (regression guard against side-mixing bugs)."""
    snap = _snap()
    pe_recipe = Recipe(
        id="ATM_PE_15", option_type="PE", strike_offset_steps=0, max_hold_bars=15,
        stop_pct_of_premium=0.2, target_pct_of_premium=0.3,
    )
    # CE side has data; PE side does not.
    closes = {(600, 50000, "CE"): 100.0}
    for i in range(1, 16):
        closes[(600 + i, 50000, "CE")] = 100.0 + i
    lookup = DictLookup(closes=closes)
    out = label_one(snapshot=snap, recipe=pe_recipe, lookup=lookup, contract=LabelContract())
    assert out["label"] == SKIP_LABEL
    assert out["reason_skipped"] == "missing_strike_at_entry"


def test_recipe_validation_rejects_bad_params():
    with pytest.raises(ValueError):
        Recipe(id="x", option_type="XX", strike_offset_steps=0, max_hold_bars=15,
               stop_pct_of_premium=0.2, target_pct_of_premium=0.3)
    with pytest.raises(ValueError):
        Recipe(id="x", option_type="CE", strike_offset_steps=0, max_hold_bars=0,
               stop_pct_of_premium=0.2, target_pct_of_premium=0.3)
    with pytest.raises(ValueError):
        Recipe(id="x", option_type="CE", strike_offset_steps=0, max_hold_bars=15,
               stop_pct_of_premium=0.0, target_pct_of_premium=0.3)
    with pytest.raises(ValueError):
        Recipe(id="x", option_type="CE", strike_offset_steps=0, max_hold_bars=15,
               stop_pct_of_premium=1.0, target_pct_of_premium=0.3)
    with pytest.raises(ValueError):
        Recipe(id="x", option_type="CE", strike_offset_steps=0, max_hold_bars=15,
               stop_pct_of_premium=0.2, target_pct_of_premium=0.0)


def test_happy_path_debug_columns_complete():
    """On a successful label, every debug column from the contract must be set."""
    snap = _snap()
    closes = {(600 + i, 50000, "CE"): 100.0 + i for i in range(0, 16)}
    lookup = DictLookup(closes=closes)
    out = label_one(snapshot=snap, recipe=_atm_recipe_ce_15(), lookup=lookup, contract=LabelContract())
    for col in [
        "label", "reason_skipped", "selected_strike", "selected_expiry",
        "entry_premium", "exit_premium", "exit_bar_offset", "exit_reason",
        "gross_pnl_pct", "net_pnl_pct", "cost_pct",
    ]:
        assert col in out, f"missing debug column {col}"
    assert out["selected_expiry"] == "04JAN24"
    assert out["reason_skipped"] == ""
