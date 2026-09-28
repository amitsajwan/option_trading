"""Tests for the pure helpers in
ml_pipeline_2.scripts.study_asymmetric_exit_leg_split_banknifty."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.variable_hold_exit import ExitGridParams
from ml_pipeline_2.scripts.study_asymmetric_exit_leg_split_banknifty import (
    leg_combo_rows,
    window_underlying_drift,
)

GRID = [
    ExitGridParams(stop_loss_pct=0.05, trail_activate_pct=0.10, trail_distance_pct=0.20),
    ExitGridParams(stop_loss_pct=0.10, trail_activate_pct=0.20, trail_distance_pct=0.30),
]


class TestLegComboRows:
    def test_consistently_positive_combo_detected(self):
        # combo 0 positive in every window, combo 1 negative in every window
        windows = [np.column_stack([np.full(30, 0.02), np.full(30, -0.03)]) for _ in range(5)]
        rows = leg_combo_rows(windows, GRID, recent_n=4, n_boot=200)
        assert rows[0]["consistency"]["all_windows_positive"] is True
        assert rows[1]["consistency"]["all_windows_positive"] is False
        assert rows[0]["pooled_stats"]["expectancy"] == pytest.approx(0.02)
        assert rows[1]["pooled_stats"]["expectancy"] == pytest.approx(-0.03)

    def test_nan_rows_dropped_not_imputed(self):
        arr = np.column_stack([np.array([0.1, np.nan, -0.1, np.nan]), np.array([0.0, np.nan, 0.0, np.nan])])
        rows = leg_combo_rows([arr], GRID, recent_n=1, min_rows=1, n_boot=100)
        assert rows[0]["pooled_stats"]["n"] == 2
        assert rows[0]["pooled_stats"]["expectancy"] == pytest.approx(0.0)

    def test_profit_factor(self):
        arr = np.column_stack([np.array([0.3, -0.1, -0.1]), np.array([0.1, -0.2, 0.1])])
        rows = leg_combo_rows([arr], GRID, recent_n=1, min_rows=1, n_boot=100)
        assert rows[0]["pooled_stats"]["profit_factor"] == pytest.approx(1.5)
        assert rows[1]["pooled_stats"]["profit_factor"] == pytest.approx(1.0)

    def test_thin_window_excluded_from_consistency(self):
        big = np.column_stack([np.full(30, 0.01), np.full(30, 0.01)])
        thin = np.column_stack([np.full(3, -0.5), np.full(3, -0.5)])
        rows = leg_combo_rows([big, thin], GRID, recent_n=2, min_rows=20, n_boot=100)
        c = rows[0]["consistency"]
        assert c["n_usable_windows"] == 1
        assert c["thin_sample_by_window"] == [False, True]
        # a thin window blocks the "all windows positive" claim
        assert c["all_windows_positive"] is False

    def test_ci_brackets_mean(self):
        rng = np.random.default_rng(0)
        arr = np.column_stack([rng.normal(0.01, 0.05, 500), rng.normal(-0.01, 0.05, 500)])
        rows = leg_combo_rows([arr], GRID, recent_n=1, n_boot=500)
        p = rows[0]["pooled_stats"]
        assert p["expectancy_ci_lo"] <= p["expectancy"] <= p["expectancy_ci_hi"]


class TestWindowUnderlyingDrift:
    def test_positive_drift(self):
        df = pd.DataFrame({"trade_date": ["2026-01-01", "2026-01-02"], "time": ["09:15:00", "09:15:00"],
                           "fut_close": [50000.0, 51000.0]})
        assert window_underlying_drift(df) == pytest.approx(0.02)

    def test_sorts_before_computing(self):
        df = pd.DataFrame({"trade_date": ["2026-01-02", "2026-01-01"], "time": ["09:15:00", "09:15:00"],
                           "fut_close": [49000.0, 50000.0]})
        assert window_underlying_drift(df) == pytest.approx(-0.02)

    def test_missing_column_or_empty(self):
        assert window_underlying_drift(pd.DataFrame({"trade_date": [], "time": []})) is None
        df = pd.DataFrame({"trade_date": ["2026-01-01"], "time": ["09:15:00"], "fut_close": [50000.0]})
        assert window_underlying_drift(df) is None
