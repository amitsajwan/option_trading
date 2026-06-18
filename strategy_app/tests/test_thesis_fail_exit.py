from __future__ import annotations

from datetime import datetime, timezone

from strategy_app.contracts import ExitReason, PositionContext
from strategy_app.position.tracker import PositionTracker


def _pos(**kwargs: object) -> PositionContext:
    base = dict(
        position_id="p1",
        direction="CE",
        strike=50000,
        expiry=None,
        entry_premium=100.0,
        entry_time=datetime(2024, 5, 2, 10, 0, tzinfo=timezone.utc),
        entry_snapshot_id="s1",
        lots=1,
        bars_held=2,
        pnl_pct=-0.10,
        mfe_pct=0.005,
        thesis_fail_exit_bars=2,
        thesis_fail_min_mfe_pct=0.02,
        thesis_fail_pnl_pct=-0.03,
        early_stop_loss_bars=2,
        early_stop_loss_pct=0.12,
    )
    base.update(kwargs)
    return PositionContext(**base)


def test_early_stop_within_two_bars() -> None:
    assert PositionTracker._is_early_stop_hit(_pos(pnl_pct=-0.13, bars_held=2))


def test_thesis_fail_when_no_run_and_red() -> None:
    assert PositionTracker._is_thesis_fail_exit(_pos(pnl_pct=-0.09, mfe_pct=0.01, bars_held=2))


def test_thesis_fail_not_before_min_bars() -> None:
    assert not PositionTracker._is_thesis_fail_exit(_pos(bars_held=1))
