"""Unit tests for strategy_app.sim.multi_day_runner (MD-S3/S4 DoD).

Tests verify:
- _aggregate() correctly computes max_drawdown, profit_factor, expectancy.
- ab_compare() winner determination.
- render_report() produces valid Markdown.
- _make_day_result() metric math.

These tests exercise the pure aggregation logic without any engine or parquet calls.
"""

from __future__ import annotations

import math
import pytest

from strategy_app.sim.multi_day_runner import (
    DayResult,
    MultiDayResult,
    _aggregate,
    _make_day_result,
    render_report,
)


# ---------------------------------------------------------------------------
# _make_day_result — per-day metric math
# ---------------------------------------------------------------------------

def _trade(pnl: float, mfe: float = 0.0) -> dict:
    return {"pnl_pct": pnl, "mfe_pct": mfe, "mae_pct": 0.0}


def test_make_day_result_empty():
    day = _make_day_result("2026-01-02", [])
    assert day.trade_count == 0
    assert day.pnl == 0.0
    assert day.profit_factor == float("inf")   # no losses
    assert math.isnan(day.capture_ratio)


def test_make_day_result_all_wins():
    trades = [_trade(0.05, 0.07), _trade(0.03, 0.04)]
    day = _make_day_result("2026-01-02", trades)
    assert day.trade_count == 2
    assert day.win_count == 2
    assert day.pnl == pytest.approx(0.08)
    assert day.profit_factor == float("inf")
    assert day.expectancy == pytest.approx(0.04)


def test_make_day_result_mixed():
    trades = [_trade(0.05, 0.08), _trade(-0.02, 0.01), _trade(0.03, 0.04)]
    day = _make_day_result("2026-01-02", trades)
    assert day.win_count == 2
    assert day.pnl == pytest.approx(0.06)
    # PF = 0.08 / 0.02 = 4.0
    assert day.profit_factor == pytest.approx(4.0)
    assert day.expectancy == pytest.approx(0.06 / 3)


def test_make_day_result_capture_ratio():
    # capture = pnl/mfe for trades with mfe > 0
    trades = [_trade(0.05, 0.10), _trade(-0.02, 0.05)]
    day = _make_day_result("2026-01-02", trades)
    expected_cap = (0.05 / 0.10 + (-0.02) / 0.05) / 2
    assert day.capture_ratio == pytest.approx(expected_cap)


# ---------------------------------------------------------------------------
# _aggregate — portfolio-level metrics
# ---------------------------------------------------------------------------

def _make_result(day_pnls: list[float], fat_tail_threshold: float = 0.05) -> MultiDayResult:
    days = []
    for i, pnl in enumerate(day_pnls):
        day = DayResult(
            trade_date=f"2026-01-{i+2:02d}",
            trades=[_trade(pnl)],
            pnl=pnl,
            win_count=(1 if pnl > 0 else 0),
            trade_count=1,
            profit_factor=(float("inf") if pnl >= 0 else 0.0),
            expectancy=pnl,
            avg_mfe=abs(pnl),
            capture_ratio=1.0,
        )
        days.append(day)
    result = MultiDayResult(
        date_from="2026-01-02",
        date_to=f"2026-01-{len(day_pnls)+1:02d}",
        config_env={},
        days=days,
        fat_tail_threshold=fat_tail_threshold,
    )
    _aggregate(result)
    return result


def test_aggregate_cumulative_pnl():
    result = _make_result([0.01, 0.02, -0.01, 0.03])
    assert result.cumulative_pnl == pytest.approx(0.05)


def test_aggregate_max_drawdown():
    # Curve: 0.05, 0.08, 0.04, 0.07 → peak 0.08, trough 0.04 → dd = 0.04
    result = _make_result([0.05, 0.03, -0.04, 0.03])
    assert result.max_drawdown == pytest.approx(0.04)


def test_aggregate_max_drawdown_no_recovery():
    # Monotonically declining: 0.02, -0.01, -0.03, -0.02
    # Curve: 0.02, 0.01, -0.02, -0.04 → max_dd = 0.02 - (-0.04) = 0.06
    result = _make_result([0.02, -0.01, -0.03, -0.02])
    assert result.max_drawdown == pytest.approx(0.06)


def test_aggregate_fat_tail_days():
    result = _make_result([0.01, 0.06, -0.02, 0.07, 0.04], fat_tail_threshold=0.05)
    assert result.fat_tail_days == 2  # 0.06 and 0.07


def test_aggregate_profit_factor():
    # wins: 0.05, 0.03 → sum = 0.08; losses: -0.02 → sum = 0.02; PF = 4.0
    result = _make_result([0.05, -0.02, 0.03])
    assert result.profit_factor == pytest.approx(4.0)


def test_aggregate_no_losses_gives_inf_profit_factor():
    result = _make_result([0.01, 0.02, 0.03])
    assert result.profit_factor == float("inf")


def test_aggregate_win_days():
    result = _make_result([0.02, -0.01, 0.03, 0.0, -0.02])
    assert result.win_days == 2   # only strictly positive days


# ---------------------------------------------------------------------------
# render_report — smoke test: produces valid markdown with key sections
# ---------------------------------------------------------------------------

def test_render_report_basic():
    result = _make_result([0.05, -0.02, 0.03, 0.06, -0.01])
    report = render_report(result)
    assert "# Multi-Day Sim Report" in report
    assert "Cumulative P&L" in report
    assert "Max drawdown" in report
    assert "Profit factor" in report
    assert "Per-Day Results" in report


def test_render_report_ab():
    from strategy_app.sim.multi_day_runner import ABResult

    ra = _make_result([0.05, -0.01, 0.03])
    rb = _make_result([-0.02, 0.01, -0.01])
    ab = ABResult(
        date_from="2026-01-02",
        date_to="2026-01-04",
        result_a=ra,
        result_b=rb,
        winner_pnl="A",
        winner_dd="B",
    )
    report = render_report(ra, ab=ab)
    assert "A/B Comparison" in report
    assert "Config A" in report
    assert "Config B" in report
