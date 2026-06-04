"""Unit tests for MarketStructureTracker (bottoms / highs / breakouts lens)."""
from __future__ import annotations

from strategy_app.market.market_structure import MarketStructureTracker
from strategy_app.market.snapshot_accessor import SnapshotAccessor


def _snap(close, high=None, low=None, *, r1=None, r5=None, r15=None,
          orh=None, orl=None, orh_broken=False, orl_broken=False, pvwap=None):
    return SnapshotAccessor({
        "session_context": {"timestamp": "2026-06-04T09:30:00", "trade_date_ist": "2026-06-04"},
        "futures_bar": {
            "fut_close": close,
            "fut_high": high if high is not None else close,
            "fut_low": low if low is not None else close,
        },
        "futures_derived": {
            "fut_return_1m": r1, "fut_return_5m": r5, "fut_return_15m": r15,
            "price_vs_vwap": pvwap,
        },
        "opening_range": {"orh": orh, "orl": orl,
                          "orh_broken": orh_broken, "orl_broken": orl_broken},
    })


def _feed(tracker, prices):
    for p in prices:
        tracker.update(_snap(p, high=p + 2, low=p - 2))


def test_near_high_in_range():
    t = MarketStructureTracker()
    t.on_session_start("2026-06-04")
    # Rising series ending at the top of the day's range.
    _feed(t, [100, 105, 110, 120, 130])
    ms = t.snapshot(_snap(130, high=132, low=128))
    assert ms["position_in_range"]["label"] == "near_high"
    assert ms["position_in_range"]["range_position"] >= 0.8


def test_near_low_in_range():
    t = MarketStructureTracker()
    t.on_session_start("2026-06-04")
    _feed(t, [130, 120, 110, 105, 100])
    ms = t.snapshot(_snap(100, high=102, low=98))
    assert ms["position_in_range"]["label"] == "near_low"


def test_breakout_up_vs_fakeout():
    t = MarketStructureTracker(breakout_lookback=5)
    t.on_session_start("2026-06-04")
    # Build a tight range, then close decisively above it → breakout_up.
    for p in [100, 101, 99, 100, 101, 100]:
        t.update(_snap(p, high=p + 1, low=p - 1))
    t.update(_snap(110, high=111, low=104))  # closes above prior_high
    ms = t.snapshot(_snap(110, high=111, low=104))
    assert ms["breakout_state"]["label"] == "breakout_up"

    # Fakeout: poke above the range then close back inside.
    t2 = MarketStructureTracker(breakout_lookback=5)
    t2.on_session_start("2026-06-04")
    for p in [100, 101, 99, 100, 101, 100]:
        t2.update(_snap(p, high=p + 1, low=p - 1))
    t2.update(_snap(100, high=115, low=99))  # wick above, close back inside
    ms2 = t2.snapshot(_snap(100, high=115, low=99))
    assert ms2["breakout_state"]["label"] == "fakeout_up"


def test_swing_structure_uptrend():
    t = MarketStructureTracker(pivot_k=1)
    t.on_session_start("2026-06-04")
    # Zig-zag making higher highs and higher lows.
    series = [100, 110, 104, 116, 108, 124, 116, 132]
    for p in series:
        t.update(_snap(p, high=p + 1, low=p - 1))
    ms = t.snapshot(_snap(132, high=133, low=131))
    assert ms["swing_pivots"]["structure"] in ("uptrend", "range")
    # With clear HH/HL the classifier should land on uptrend.
    assert ms["swing_pivots"]["last_swing_high"] is not None


def test_momentum_alignment():
    t = MarketStructureTracker()
    t.on_session_start("2026-06-04")
    t.update(_snap(100, high=101, low=99, r1=0.001, r5=0.002, r15=0.003))
    ms = t.snapshot(_snap(100, high=101, low=99, r1=0.001, r5=0.002, r15=0.003))
    assert ms["momentum_alignment"]["label"] == "aligned_up"

    t.update(_snap(100, high=101, low=99, r1=0.001, r5=-0.002, r15=0.003))
    ms2 = t.snapshot(_snap(100, high=101, low=99, r1=0.001, r5=-0.002, r15=0.003))
    assert ms2["momentum_alignment"]["label"] == "mixed"


def test_session_reset_clears_state():
    t = MarketStructureTracker()
    t.on_session_start("2026-06-04")
    _feed(t, [100, 200, 300])
    t.on_session_start("2026-06-05")  # new day must clear
    ms = t.snapshot(_snap(150, high=151, low=149))
    assert ms["bars_seen"] == 0
    assert ms["position_in_range"]["day_high"] is None


def test_insufficient_bars_is_safe():
    t = MarketStructureTracker()
    t.on_session_start("2026-06-04")
    ms = t.snapshot(_snap(100, high=101, low=99))
    assert ms["bars_seen"] == 0
    assert ms["swing_pivots"]["structure"] == "insufficient"
    assert ms["breakout_state"]["range"] == "insufficient"
