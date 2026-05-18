"""Unit tests for the trainer's single-position simulation.

The realistic per-recipe expectation depends on this honoring exit_bar_offset
correctly. Bugs here would silently inflate or deflate the "what should the
runtime produce" baseline, which is the primary metric we use to decide
deployment-readiness.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ml_pipeline_2.scripts.train_option_pnl_mvp import simulate_single_position


def _row(date, minute, pnl, hold):
    return {
        "trade_date": pd.Timestamp(date),
        "timestamp_minute": minute,
        "net_pnl_pct": pnl,
        "exit_bar_offset": hold,
    }


def test_single_position_fires_first_crossing_then_blocks():
    """Two prob-crossings within hold window: only first fires."""
    df = pd.DataFrame([
        _row("2024-08-01", 600, 0.05, 15),
        _row("2024-08-01", 605, 0.07, 15),  # blocked: 600+15=615 > 605
        _row("2024-08-01", 614, 0.03, 15),  # blocked
        _row("2024-08-01", 616, 0.10, 15),  # passes: 600+15=615 ≤ 616
    ])
    probs = np.array([0.60, 0.65, 0.70, 0.62])
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 2
    assert out["net_pnl_sum"] == 0.05 + 0.10
    assert out["win_rate"] == 1.0


def test_single_position_resets_on_date_boundary():
    """Position state must reset at trade_date boundary (overnight gap)."""
    df = pd.DataFrame([
        _row("2024-08-01", 900, 0.04, 30),  # would block until 930
        _row("2024-08-02", 915, 0.06, 15),  # different day → fires
    ])
    probs = np.array([0.60, 0.60])
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 2
    assert out["net_pnl_sum"] == 0.04 + 0.06


def test_single_position_skips_below_threshold():
    df = pd.DataFrame([
        _row("2024-08-01", 600, 0.05, 15),
        _row("2024-08-01", 700, 0.03, 15),
    ])
    probs = np.array([0.40, 0.60])  # first below thr, second above
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 1
    assert out["net_pnl_sum"] == 0.03


def test_single_position_uses_actual_hold_bars():
    """If exit_bar_offset is short (early stop hit), next fire allowed sooner."""
    df = pd.DataFrame([
        _row("2024-08-01", 600, -0.10, 3),   # stopped out at +3 bars
        _row("2024-08-01", 604, 0.05, 15),  # 600+3=603 ≤ 604 → fires
        _row("2024-08-01", 606, 0.07, 15),  # blocked (604+15=619 > 606)
    ])
    probs = np.array([0.60, 0.60, 0.60])
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 2
    assert out["net_pnl_sum"] == -0.10 + 0.05


def test_single_position_empty_input():
    df = pd.DataFrame([_row("2024-08-01", 600, 0.05, 15)])
    out = simulate_single_position(df, np.array([0.30]), threshold=0.55)
    assert out["n_trades"] == 0
    assert out["net_pnl_sum"] == 0.0
    assert out["win_rate"] == 0.0


def test_single_position_correctly_orders_rows():
    """Even if input rows are scrambled, simulation must walk in time order."""
    df = pd.DataFrame([
        _row("2024-08-01", 700, 0.05, 15),
        _row("2024-08-01", 600, 0.07, 15),  # earlier — should fire first
        _row("2024-08-01", 800, 0.03, 15),
    ])
    probs = np.array([0.60, 0.65, 0.70])
    out = simulate_single_position(df, probs, threshold=0.55)
    # First fire at minute 600 (pnl 0.07, blocks to 615);
    # 700 ≥ 615 → fires (pnl 0.05, blocks to 715);
    # 800 ≥ 715 → fires (pnl 0.03)
    assert out["n_trades"] == 3
    assert out["net_pnl_sum"] == 0.07 + 0.05 + 0.03


def test_single_position_win_rate_calculation():
    df = pd.DataFrame([
        _row("2024-08-01", 600, +0.10, 15),
        _row("2024-08-02", 600, -0.05, 15),
        _row("2024-08-03", 600, +0.02, 15),
    ])
    probs = np.array([0.60, 0.60, 0.60])
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 3
    assert out["win_rate"] == pytest_approx(2 / 3)


# Tiny local approx helper to avoid importing pytest in this scope when run
# under bare unittest. Actual pytest is invoked via test command.
def pytest_approx(value, rel=1e-6):
    class _Approx:
        def __eq__(self, other):
            return abs(other - value) <= max(abs(value), abs(other)) * rel
        def __repr__(self):
            return f"~={value}"
    return _Approx()
