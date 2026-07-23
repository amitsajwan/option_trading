"""_is_underlying_stop_hit / _is_underlying_target_hit must be position_side-aware
(found 2026-07-22 review). Both functions branched only on direction (CE/PE),
assuming CE==bullish-long / PE==bearish-long -- backwards for a SHORT position
(e.g. _r1s_short_ce, _playbook_brain): a short CE profits when the underlying
falls and loses when it rises, so its stop must fire on a RISE, not a fall.
"""
from __future__ import annotations

from datetime import datetime, timezone

from strategy_app.contracts import PositionContext
from strategy_app.position.tracker import PositionTracker

ENTRY_FUT = 57200.0
STOP_PCT = 0.01    # 1%
TARGET_PCT = 0.02  # 2%


def _pos(direction: str, position_side: str) -> PositionContext:
    return PositionContext(
        position_id="pos-001",
        direction=direction,
        strike=57200,
        expiry=None,
        entry_premium=100.0,
        entry_time=datetime(2026, 7, 22, 9, 30, tzinfo=timezone.utc),
        entry_snapshot_id="snap-001",
        lots=1,
        position_side=position_side,
        entry_futures_price=ENTRY_FUT,
        underlying_stop_pct=STOP_PCT,
        underlying_target_pct=TARGET_PCT,
    )


def test_long_ce_stop_fires_on_fall_not_rise():
    tr = PositionTracker()
    pos = _pos("CE", "LONG")
    fallen = ENTRY_FUT * (1.0 - STOP_PCT - 0.001)
    risen = ENTRY_FUT * (1.0 + STOP_PCT + 0.001)
    assert tr._is_underlying_stop_hit(pos, fallen) is True
    assert tr._is_underlying_stop_hit(pos, risen) is False


def test_short_ce_stop_fires_on_rise_not_fall():
    """A short call loses when the underlying rises -- stop must fire there,
    not on a fall (which is actually favorable to the position)."""
    tr = PositionTracker()
    pos = _pos("CE", "SHORT")
    fallen = ENTRY_FUT * (1.0 - STOP_PCT - 0.001)
    risen = ENTRY_FUT * (1.0 + STOP_PCT + 0.001)
    assert tr._is_underlying_stop_hit(pos, risen) is True
    assert tr._is_underlying_stop_hit(pos, fallen) is False


def test_short_ce_target_fires_on_fall_not_rise():
    """A short call's winning direction is the underlying falling/staying flat."""
    tr = PositionTracker()
    pos = _pos("CE", "SHORT")
    fallen = ENTRY_FUT * (1.0 - TARGET_PCT - 0.001)
    risen = ENTRY_FUT * (1.0 + TARGET_PCT + 0.001)
    assert tr._is_underlying_target_hit(pos, fallen) is True
    assert tr._is_underlying_target_hit(pos, risen) is False


def test_long_pe_stop_fires_on_rise_not_fall():
    tr = PositionTracker()
    pos = _pos("PE", "LONG")
    fallen = ENTRY_FUT * (1.0 - STOP_PCT - 0.001)
    risen = ENTRY_FUT * (1.0 + STOP_PCT + 0.001)
    assert tr._is_underlying_stop_hit(pos, risen) is True
    assert tr._is_underlying_stop_hit(pos, fallen) is False


def test_short_pe_stop_fires_on_fall_not_rise():
    """A short put loses when the underlying falls -- stop must fire there."""
    tr = PositionTracker()
    pos = _pos("PE", "SHORT")
    fallen = ENTRY_FUT * (1.0 - STOP_PCT - 0.001)
    risen = ENTRY_FUT * (1.0 + STOP_PCT + 0.001)
    assert tr._is_underlying_stop_hit(pos, fallen) is True
    assert tr._is_underlying_stop_hit(pos, risen) is False


def test_short_pe_target_fires_on_rise_not_fall():
    """A short put's winning direction is the underlying rising/staying flat."""
    tr = PositionTracker()
    pos = _pos("PE", "SHORT")
    fallen = ENTRY_FUT * (1.0 - TARGET_PCT - 0.001)
    risen = ENTRY_FUT * (1.0 + TARGET_PCT + 0.001)
    assert tr._is_underlying_target_hit(pos, risen) is True
    assert tr._is_underlying_target_hit(pos, fallen) is False
