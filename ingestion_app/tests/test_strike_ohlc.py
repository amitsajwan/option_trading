"""Tests for the per-strike 1-min OHLC accumulator (forward exit-fidelity fix)."""
from __future__ import annotations

from ingestion_app.strike_ohlc import StrikeOhlcAccumulator

M = 60.0   # one minute


def test_single_minute_builds_ohlc():
    acc = StrikeOhlcAccumulator()
    base = 600_000.0  # minute-aligned (600000 / 60 = 10000.0)
    for px, dt in [(100.0, 0), (120.0, 5), (95.0, 10), (110.0, 55)]:
        acc.update(54000, "CE", px, base + dt)
    bar = acc.bar(54000, "CE")
    assert bar == {"open": 100.0, "high": 120.0, "low": 95.0, "close": 110.0}


def test_minute_rollover_finalizes_previous():
    acc = StrikeOhlcAccumulator()
    base = 600_000.0  # minute-aligned
    acc.update(54000, "CE", 100.0, base + 10)
    acc.update(54000, "CE", 130.0, base + 50)      # minute 0: O100 H130 L100 C130
    acc.update(54000, "CE", 90.0, base + M + 5)    # minute 1 starts
    # current bar is minute 1
    assert acc.bar(54000, "CE") == {"open": 90.0, "high": 90.0, "low": 90.0, "close": 90.0}
    # the completed minute 0 is retrievable by its (floored-epoch) minute key
    prev = acc.bar(54000, "CE", prefer_minute=int(base))
    assert prev == {"open": 100.0, "high": 130.0, "low": 100.0, "close": 130.0}


def test_ce_and_pe_tracked_independently():
    acc = StrikeOhlcAccumulator()
    acc.update(54000, "CE", 200.0, 0)
    acc.update(54000, "PE", 50.0, 0)
    acc.update(54000, "CE", 210.0, 5)
    acc.update(54000, "PE", 45.0, 5)
    assert acc.bar(54000, "CE")["high"] == 210.0
    assert acc.bar(54000, "PE")["low"] == 45.0


def test_skips_none_nan_nonpositive():
    acc = StrikeOhlcAccumulator()
    acc.update(54000, "CE", None, 0)
    acc.update(54000, "CE", float("nan"), 1)
    acc.update(54000, "CE", -5.0, 2)
    assert acc.bar(54000, "CE") is None      # nothing valid recorded
    acc.update(54000, "CE", 100.0, 3)
    assert acc.bar(54000, "CE")["open"] == 100.0


def test_prune_drops_old_minutes():
    acc = StrikeOhlcAccumulator()
    acc.update(54000, "CE", 100.0, 0)            # minute 0
    acc.update(54000, "CE", 110.0, 10 * M)       # jump to minute 10
    acc.prune(before_epoch_s=9 * M)              # drop anything older than minute 9
    # current (minute 10) survives; the old completed minute-0 bar is pruned
    assert acc.bar(54000, "CE") == {"open": 110.0, "high": 110.0, "low": 110.0, "close": 110.0}
    assert acc.bar(54000, "CE", prefer_minute=0) is None


def test_bar_absent_strike_is_none():
    assert StrikeOhlcAccumulator().bar(99999, "CE") is None
