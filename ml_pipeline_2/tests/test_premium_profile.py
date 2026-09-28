"""Tests for ml_pipeline_2.pipeline.premium_profile (BankNifty research,
2026-09-27) -- pure aggregation functions, no snapshot/parquet fixture
needed."""
from __future__ import annotations

import numpy as np
import pytest

from ml_pipeline_2.pipeline.premium_profile import (
    entry_premium_level_summary,
    path_extremes,
    summarize_extremes,
)


class TestPathExtremes:
    def test_pure_gain_path(self):
        # entry=100, path rises monotonically to 130 -> max_gain=0.30; the
        # path never revisits (or falls below) the entry price itself, so
        # the "drawdown" (the minimum relative value anywhere on the path)
        # is the smallest STILL-POSITIVE point (+0.05 at price=105), not 0.0
        # -- 0.0 would require the path to actually touch the entry price.
        out = path_extremes(100.0, [105, 110, 120, 130])
        assert out["max_gain_pct"] == pytest.approx(0.30)
        assert out["max_drawdown_pct"] == pytest.approx(0.05)

    def test_pure_loss_path(self):
        # entry=100, path only ever falls -> max_gain_pct is the SMALLEST
        # loss point on the path (-0.05 at price=95, still the best the
        # path ever got relative to entry), max_drawdown_pct the worst (-0.20).
        out = path_extremes(100.0, [95, 90, 80, 85])
        assert out["max_gain_pct"] == pytest.approx(-0.05)
        assert out["max_drawdown_pct"] == pytest.approx(-0.20)

    def test_mixed_path_peak_then_crash(self):
        # rallies to +40% then crashes to -10% relative to entry
        out = path_extremes(100.0, [140, 120, 90, 95])
        assert out["max_gain_pct"] == pytest.approx(0.40)
        assert out["max_drawdown_pct"] == pytest.approx(-0.10)

    def test_flat_path(self):
        out = path_extremes(50.0, [50, 50, 50])
        assert out["max_gain_pct"] == pytest.approx(0.0)
        assert out["max_drawdown_pct"] == pytest.approx(0.0)

    def test_empty_path_raises(self):
        with pytest.raises(ValueError):
            path_extremes(100.0, [])

    def test_non_positive_entry_raises(self):
        with pytest.raises(ValueError):
            path_extremes(0.0, [10, 20])
        with pytest.raises(ValueError):
            path_extremes(-5.0, [10, 20])


class TestSummarizeExtremes:
    def test_empty_rows(self):
        assert summarize_extremes([]) == {"n": 0}

    def test_basic_aggregation(self):
        rows = [
            {"max_gain_pct": 0.05, "max_drawdown_pct": -0.02},
            {"max_gain_pct": 0.15, "max_drawdown_pct": -0.08},
            {"max_gain_pct": 0.30, "max_drawdown_pct": -0.15},
            {"max_gain_pct": 0.10, "max_drawdown_pct": -0.05},
        ]
        out = summarize_extremes(rows)
        assert out["n"] == 4
        assert out["mean_max_gain_pct"] == pytest.approx(np.mean([0.05, 0.15, 0.30, 0.10]))
        assert out["mean_max_drawdown_pct"] == pytest.approx(np.mean([-0.02, -0.08, -0.15, -0.05]))
        assert "p50" in out["max_gain_pct"]
        assert "p50" in out["max_drawdown_pct"]

    def test_stop_hit_rates_monotonic_in_stop_size(self):
        # Tighter stops should be hit at least as often as looser ones.
        rows = [
            {"max_gain_pct": 0.02, "max_drawdown_pct": d}
            for d in (-0.01, -0.06, -0.12, -0.25, -0.50)
        ]
        out = summarize_extremes(rows, stop_grid=(0.05, 0.10, 0.20, 0.40))
        rates = out["stop_hit_rates"]
        assert rates["stop_0.05"] >= rates["stop_0.10"] >= rates["stop_0.20"] >= rates["stop_0.40"]
        # -0.50 breaches every stop level in the grid
        assert rates["stop_0.40"] >= 0.2

    def test_activate_hit_rates_monotonic_in_activate_size(self):
        rows = [
            {"max_gain_pct": g, "max_drawdown_pct": -0.01}
            for g in (0.02, 0.08, 0.18, 0.35, 0.60)
        ]
        out = summarize_extremes(rows, activate_grid=(0.10, 0.20, 0.30))
        rates = out["activate_hit_rates"]
        assert rates["activate_0.10"] >= rates["activate_0.20"] >= rates["activate_0.30"]

    def test_no_grid_args_omits_hit_rate_keys_but_still_present_empty(self):
        rows = [{"max_gain_pct": 0.1, "max_drawdown_pct": -0.1}]
        out = summarize_extremes(rows)
        assert out["stop_hit_rates"] == {}
        assert out["activate_hit_rates"] == {}


class TestEntryPremiumLevelSummary:
    def test_empty(self):
        assert entry_premium_level_summary([]) == {"n": 0}

    def test_drops_none_and_non_positive(self):
        out = entry_premium_level_summary([100.0, None, 0.0, -5.0, 200.0])
        assert out["n"] == 2
        assert out["mean"] == pytest.approx(150.0)
        assert out["min"] == pytest.approx(100.0)
        assert out["max"] == pytest.approx(200.0)

    def test_percentiles_reasonable(self):
        values = list(range(10, 210, 10))  # 10..200
        out = entry_premium_level_summary(values)
        assert out["n"] == len(values)
        assert out["p10"] < out["median"] < out["p90"]
