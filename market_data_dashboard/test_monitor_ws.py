"""Regression test for the dashboard's live/replay monitor KPI tiles.

Born 2026-09-20: _build_kpi_live/_build_kpi_replay's "SESSION P&L" tile
pre-formats a fraction (MonitorTrade.pnlPct, e.g. 0.05 == 5%) into a final
display string server-side, which bypasses this repo's own documented
contract for that field ("Keep full fractional precision; UI fmtPct
multiplies by 100 at display" -- see market_data_dashboard/real_source.py).
The bug understated every operator-visible session P&L KPI by exactly
100x. No test existed for this function at all, which is how a live,
wired-in (market_data_dashboard/app.py imports this router) bug went
unnoticed.
"""
from __future__ import annotations

from types import SimpleNamespace

from market_data_dashboard.routes.monitor_ws import _build_kpi_live


def _fake_trade(pnl_pct: float, exit_idx: int) -> SimpleNamespace:
    return SimpleNamespace(pnlPct=pnl_pct, exitIdx=exit_idx)


def _fake_state(trades, current_idx: int = 100) -> SimpleNamespace:
    session = SimpleNamespace(
        trades=trades,
        candles=[SimpleNamespace()] * (current_idx + 1),
        engine="deterministic",
        instrument="NIFTY",
    )
    return SimpleNamespace(session=session, current_idx=current_idx, last_price=100.0)


def _kpi_value(items, label: str) -> str:
    for item in items:
        if item.label == label:
            return item.value
    raise AssertionError(f"no KPI item with label={label!r}")


def test_session_pnl_kpi_scales_fraction_to_percent():
    # Two trades summing to a 0.11 fraction -- must render as +11.00%, not +0.11%.
    trades = [_fake_trade(0.05, exit_idx=10), _fake_trade(0.06, exit_idx=20)]
    state = _fake_state(trades)
    kpis = _build_kpi_live(state)
    assert _kpi_value(kpis, "SESSION P&L") == "+11.00%"


def test_session_pnl_kpi_only_counts_closed_trades():
    # exitIdx beyond current_idx must be excluded (position still open).
    trades = [_fake_trade(0.05, exit_idx=10), _fake_trade(0.50, exit_idx=999)]
    state = _fake_state(trades, current_idx=100)
    kpis = _build_kpi_live(state)
    assert _kpi_value(kpis, "SESSION P&L") == "+5.00%"
