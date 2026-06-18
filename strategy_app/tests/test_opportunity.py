"""Tests for the opportunity scorer (causal, session-relative selection)."""
from __future__ import annotations

from datetime import datetime, timedelta

from strategy_app.engines.opportunity import (
    BarInputs,
    OpportunityConfig,
    OpportunitySession,
)

T0 = datetime(2026, 6, 12, 9, 30)


def _bar(i, atr, *, spot=54000.0, vol=1000.0, straddle=600.0, rq=0.5):
    return BarInputs(ts=T0 + timedelta(minutes=i), spot=spot, atr_ratio=atr,
                     volume=vol, straddle_premium=straddle, regime_quality=rq)


def _cfg(**kw):
    base = dict(warmup_bars=5, selection_percentile=80.0, min_expected_move_pts=108.0,
                max_entries_per_day=3, min_spacing_minutes=20)
    base.update(kw)
    return OpportunityConfig(**base)


def test_config_from_env_overrides_daily_budget_and_spacing():
    cfg = OpportunityConfig.from_env({
        "OPPORTUNITY_GATE_ENABLED": "1",
        "OPP_GATE_MAX_ENTRIES": "6",
        "OPP_GATE_MIN_SPACING_MINUTES": "5",
    })
    assert cfg.enabled is True
    assert cfg.max_entries_per_day == 6
    assert cfg.min_spacing_minutes == 5


def test_warmup_blocks_early_bars():
    s = OpportunitySession(_cfg(warmup_bars=5))
    for i in range(5):
        d = s.observe(_bar(i, 0.0006))
        assert not d.enter and d.reason == "warmup"


_ATR_ONLY = dict(w_atr_pct=1.0, w_atr_accel=0.0, w_volume_pct=0.0,
                 w_straddle_expansion=0.0, w_regime_quality=0.0)


def test_high_score_bar_selected_after_warmup():
    s = OpportunitySession(_cfg(warmup_bars=5, **_ATR_ONLY))
    # decreasing pre-bars so none rank top (no early entries), then a clear spike
    for i, atr in enumerate([0.0010, 0.0009, 0.0008, 0.0007, 0.0006,
                             0.00055, 0.0005, 0.00045, 0.0004]):
        s.observe(_bar(i, atr))
    d = s.observe(_bar(9, 0.0020))          # spike → top of session
    assert d.enter, d.reason
    assert d.score_rank_pct >= 80.0


def test_cost_floor_blocks_relative_peak_on_dead_day():
    # A dead day: relative selection picks the day's peak, but its horizon-matched
    # expected move (atr*sqrt(hold)) is below the 108pt cost floor → no trade.
    # peak 0.00050 * 54000 = 27 pts/min * sqrt(10) ≈ 85 pts < 108.
    s = OpportunitySession(_cfg(warmup_bars=5, **_ATR_ONLY))
    for i, atr in enumerate([0.00045, 0.00043, 0.00041, 0.00039, 0.00037,
                             0.00035, 0.00033, 0.00031]):
        s.observe(_bar(i, atr, spot=54000.0))
    d = s.observe(_bar(8, 0.00050, spot=54000.0))   # relative peak, but sub-cost
    assert not d.enter and d.reason == "below_cost_floor", (d.reason, d.expected_move_pts)


def test_budget_caps_entries_per_day():
    s = OpportunitySession(_cfg(warmup_bars=3, max_entries_per_day=2, min_spacing_minutes=0))
    for i, atr in enumerate([0.0010, 0.0008, 0.0006, 0.0004]):  # decreasing → no early entry
        s.observe(_bar(i, atr))
    taken = 0
    for i in range(4, 30):                  # many strong bars (all top-ranked)
        if s.observe(_bar(i, 0.0020)).enter:
            taken += 1
    assert taken == 2                        # capped by budget


def test_min_spacing_enforced():
    s = OpportunitySession(_cfg(warmup_bars=3, max_entries_per_day=5, min_spacing_minutes=20))
    for i, atr in enumerate([0.0010, 0.0008, 0.0006, 0.0004]):  # decreasing → no early entry
        s.observe(_bar(i, atr))
    first = None
    second_within = False
    for i in range(4, 12):                    # consecutive strong bars, 1 min apart
        d = s.observe(_bar(i, 0.0020))
        if d.enter and first is None:
            first = i
        elif first is not None and i < first + 20 and d.reason == "min_spacing":
            second_within = True
    assert first is not None and second_within is True


def test_score_cutoff_with_baseline_stable_from_first_bar():
    # A multi-day baseline of LOW atr; a bar well above baseline scores high
    # immediately (after warmup) — no thin-morning ranking instability.
    base = {"atr": [0.0003, 0.00035, 0.0004, 0.00045, 0.0005] * 4,
            "volume": [800.0] * 20, "accel": [], "strd_exp": []}
    cfg = OpportunityConfig(warmup_bars=3, selection_mode="score_cutoff",
                            score_cutoff=60.0, min_expected_move_pts=108.0,
                            max_entries_per_day=3, min_spacing_minutes=0, **_ATR_ONLY)
    s = OpportunitySession(cfg, baseline=base)
    for i in range(4):
        s.observe(_bar(i, 0.0004))            # ~mid baseline → modest score
    d = s.observe(_bar(5, 0.0025))            # well above baseline → high score
    assert d.enter and d.reason == "selected", (d.reason, d.score)
    assert d.score >= 60.0


def test_score_cutoff_blocks_below_cutoff():
    base = {"atr": [0.0008, 0.0009, 0.0010, 0.0011, 0.0012] * 4,
            "volume": [800.0] * 20, "accel": [], "strd_exp": []}
    cfg = OpportunityConfig(warmup_bars=3, selection_mode="score_cutoff",
                            score_cutoff=70.0, min_expected_move_pts=108.0, **_ATR_ONLY)
    s = OpportunitySession(cfg, baseline=base)
    for i in range(4):
        s.observe(_bar(i, 0.0005))
    d = s.observe(_bar(5, 0.0006))            # below baseline → low score
    assert not d.enter and d.reason == "below_score_cutoff", (d.reason, d.score)


def test_disabled_never_enters():
    s = OpportunitySession(_cfg(enabled=False, warmup_bars=2))
    for i in range(10):
        assert not s.observe(_bar(i, 0.0020)).enter


def test_selection_is_relative_to_the_day():
    """A quiet day still selects its relative peaks (the whole point)."""
    s = OpportunitySession(_cfg(warmup_bars=5, **_ATR_ONLY))
    # a quiet, gently decreasing day — all well below the absolute 0.00088 gate
    for i, atr in enumerate([0.00060, 0.00055, 0.00050, 0.00045, 0.00042,
                             0.00040, 0.00038, 0.00036]):
        s.observe(_bar(i, atr))
    d = s.observe(_bar(8, 0.00065))   # relative peak for THIS day (still < 0.00088)
    assert d.enter, d.reason          # the absolute 0.00088 gate would eliminate it
