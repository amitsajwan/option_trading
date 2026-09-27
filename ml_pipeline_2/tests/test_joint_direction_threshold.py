"""Tests for joint_direction_threshold.py -- all pure functions, synthetic
fixtures only (no model, no snapshot parquet needed)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.joint_direction_threshold import (
    bootstrap_mean_ci,
    consistency_summary,
    date_concentration,
    entry_quality_mask,
    evaluate_threshold_mean,
    evaluate_threshold_with_stats,
    near_expiry_share,
    permutation_test_threshold_mean,
    pooled_percentile_thresholds,
    regime_diagnostics,
    sweep_thresholds,
)


class TestPooledPercentileThresholds:
    def test_basic_percentiles(self) -> None:
        scores = list(range(1, 101))  # 1..100
        out = pooled_percentile_thresholds(scores, percentiles=(50.0, 90.0, 99.0))
        assert len(out) == 3
        assert out[0] < out[1] < out[2]
        assert out[0] == pytest.approx(50.5, abs=1.0)

    def test_deduplicates_and_sorts(self) -> None:
        scores = [0.5] * 100
        out = pooled_percentile_thresholds(scores, percentiles=(10.0, 50.0, 90.0))
        assert out == [0.5]

    def test_empty_input(self) -> None:
        assert pooled_percentile_thresholds([]) == []

    def test_ignores_nan(self) -> None:
        scores = [1.0, 2.0, 3.0, np.nan, np.nan]
        out = pooled_percentile_thresholds(scores, percentiles=(50.0,))
        assert out == [2.0]


class TestEvaluateThresholdMean:
    def test_basic_fire_and_mean(self) -> None:
        scores = np.array([0.1, 0.9, 0.95, 0.3, 0.99])
        actual = np.array([-0.1, 0.2, 0.3, -0.05, 0.1])
        res = evaluate_threshold_mean(scores, actual, threshold=0.9, min_rows=1)
        assert res["n_fired"] == 3  # 0.9, 0.95, 0.99
        assert res["mean_actual"] == pytest.approx((0.2 + 0.3 + 0.1) / 3, abs=1e-6)
        assert res["positive"] is True
        assert res["fire_rate_of_all"] == pytest.approx(0.6)

    def test_no_fires_gives_none_mean(self) -> None:
        scores = np.array([0.1, 0.2, 0.3])
        actual = np.array([1.0, 1.0, 1.0])
        res = evaluate_threshold_mean(scores, actual, threshold=0.9)
        assert res["n_fired"] == 0
        assert res["mean_actual"] is None
        assert res["positive"] is False

    def test_negative_mean_is_not_positive(self) -> None:
        scores = np.array([0.9, 0.95])
        actual = np.array([-0.1, -0.2])
        res = evaluate_threshold_mean(scores, actual, threshold=0.9, min_rows=1)
        assert res["mean_actual"] == pytest.approx(-0.15)
        assert res["positive"] is False

    def test_base_mask_restricts_universe(self) -> None:
        scores = np.array([0.9, 0.9, 0.9, 0.9])
        actual = np.array([1.0, -1.0, 1.0, -1.0])
        base_mask = np.array([True, True, False, False])
        res = evaluate_threshold_mean(scores, actual, threshold=0.5, base_mask=base_mask, min_rows=1)
        assert res["n_universe"] == 2
        assert res["n_fired"] == 2
        assert res["mean_actual"] == pytest.approx(0.0)  # (1.0 + -1.0) / 2

    def test_nan_actual_excluded_from_mean(self) -> None:
        scores = np.array([0.9, 0.9, 0.9])
        actual = np.array([1.0, np.nan, 2.0])
        res = evaluate_threshold_mean(scores, actual, threshold=0.5, min_rows=1)
        assert res["n_fired"] == 3
        assert res["n_valid"] == 2
        assert res["mean_actual"] == pytest.approx(1.5)

    def test_thin_sample_flag(self) -> None:
        scores = np.array([0.9, 0.9])
        actual = np.array([1.0, 1.0])
        res = evaluate_threshold_mean(scores, actual, threshold=0.5, min_rows=20)
        assert res["thin_sample"] is True

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError):
            evaluate_threshold_mean([0.1, 0.2], [1.0], threshold=0.1)

    def test_mismatched_base_mask_length_raises(self) -> None:
        with pytest.raises(ValueError):
            evaluate_threshold_mean([0.1, 0.2], [1.0, 2.0], threshold=0.1, base_mask=[True])


class TestBootstrapMeanCi:
    def test_ci_brackets_true_mean_for_tight_distribution(self) -> None:
        rng = np.random.default_rng(0)
        values = rng.normal(loc=0.05, scale=0.001, size=500)
        lo, hi = bootstrap_mean_ci(values, n_boot=1000, seed=1)
        assert lo is not None and hi is not None
        assert lo < 0.05 < hi

    def test_empty_returns_none(self) -> None:
        lo, hi = bootstrap_mean_ci([])
        assert lo is None and hi is None

    def test_all_nan_returns_none(self) -> None:
        lo, hi = bootstrap_mean_ci([np.nan, np.nan])
        assert lo is None and hi is None

    def test_wider_ci_for_noisier_data(self) -> None:
        rng = np.random.default_rng(0)
        tight = rng.normal(0, 0.001, 200)
        wide = rng.normal(0, 1.0, 200)
        lo_t, hi_t = bootstrap_mean_ci(tight, seed=1)
        lo_w, hi_w = bootstrap_mean_ci(wide, seed=1)
        assert (hi_w - lo_w) > (hi_t - lo_t)


class TestPermutationTestThresholdMean:
    def test_real_signal_gives_small_p_value(self) -> None:
        rng = np.random.default_rng(0)
        n = 500
        scores = rng.uniform(0, 1, n)
        # actual strongly correlated with scores -- top-scored bars really do
        # have a higher mean.
        actual = scores * 0.5 + rng.normal(0, 0.01, n)
        threshold = np.quantile(scores, 0.9)
        observed = float(actual[scores >= threshold].mean())
        p = permutation_test_threshold_mean(scores, actual, threshold, observed, n_perm=500, seed=1)
        assert p is not None
        assert p < 0.05

    def test_no_signal_gives_large_p_value(self) -> None:
        rng = np.random.default_rng(0)
        n = 500
        scores = rng.uniform(0, 1, n)
        actual = rng.normal(0, 1, n)  # independent of scores
        threshold = np.quantile(scores, 0.9)
        observed = float(actual[scores >= threshold].mean())
        p = permutation_test_threshold_mean(scores, actual, threshold, observed, n_perm=1000, seed=1)
        assert p is not None
        assert p > 0.05

    def test_none_observed_mean_returns_none(self) -> None:
        p = permutation_test_threshold_mean([0.1, 0.9], [1.0, 2.0], 0.5, None)
        assert p is None

    def test_empty_base_mask_returns_none(self) -> None:
        p = permutation_test_threshold_mean(
            [0.1, 0.9], [1.0, 2.0], 0.5, 1.0, base_mask=[False, False],
        )
        assert p is None

    def test_restricted_to_base_mask(self) -> None:
        rng = np.random.default_rng(2)
        n = 300
        scores = rng.uniform(0, 1, n)
        actual = rng.normal(0, 1, n)
        base_mask = np.zeros(n, dtype=bool)
        base_mask[:100] = True
        threshold = 0.5
        observed = float(actual[base_mask & (scores >= threshold)].mean())
        p = permutation_test_threshold_mean(
            scores, actual, threshold, observed, base_mask=base_mask, n_perm=300, seed=5,
        )
        assert p is not None
        assert 0.0 <= p <= 1.0


class TestEvaluateThresholdWithStats:
    def test_includes_ci_and_p_value_for_real_signal(self) -> None:
        rng = np.random.default_rng(3)
        n = 400
        scores = rng.uniform(0, 1, n)
        actual = scores * 0.3 + rng.normal(0, 0.02, n)
        threshold = np.quantile(scores, 0.8)
        res = evaluate_threshold_with_stats(scores, actual, threshold, n_boot=500, n_perm=500, min_rows=1)
        assert res["mean_actual"] is not None
        assert res["ci_lo"] is not None and res["ci_hi"] is not None
        assert res["ci_lo"] <= res["mean_actual"] <= res["ci_hi"]
        assert res["permutation_p_value"] is not None

    def test_no_fires_has_no_stats(self) -> None:
        res = evaluate_threshold_with_stats([0.1, 0.2], [1.0, 1.0], threshold=0.9)
        assert res["mean_actual"] is None
        assert res["ci_lo"] is None
        assert res["permutation_p_value"] is None

    def test_single_fire_skips_stats(self) -> None:
        res = evaluate_threshold_with_stats([0.9, 0.1], [1.0, 0.0], threshold=0.5, min_rows=1)
        assert res["n_valid"] == 1
        assert res["ci_lo"] is None  # bootstrap needs >= 2 points

    def test_does_not_leak_fire_mask_key(self) -> None:
        res = evaluate_threshold_with_stats([0.9, 0.1], [1.0, 0.0], threshold=0.5)
        assert "_fire_mask" not in res


class TestSweepThresholds:
    def test_returns_one_row_per_threshold(self) -> None:
        scores = [0.1, 0.5, 0.9]
        actual = [1.0, 2.0, 3.0]
        thresholds = [0.0, 0.5, 0.95]
        rows = sweep_thresholds(scores, actual, thresholds, min_rows=1)
        assert len(rows) == 3
        assert [r["threshold"] for r in rows] == thresholds
        assert rows[0]["n_fired"] == 3
        assert rows[2]["n_fired"] == 0


class TestConsistencySummary:
    def _row(self, mean: float, *, thin: bool = False) -> dict:
        return {"mean_actual": mean, "positive": mean > 0, "thin_sample": thin}

    def test_all_positive(self) -> None:
        rows = [self._row(0.01), self._row(0.02), self._row(0.005), self._row(0.03), self._row(0.001)]
        s = consistency_summary(rows)
        assert s["all_windows_positive"] is True
        assert s["frac_positive_of_usable"] == 1.0
        assert s["n_positive_windows"] == 5

    def test_mixed_signs(self) -> None:
        rows = [self._row(0.05), self._row(-0.01), self._row(-0.02), self._row(0.001), self._row(-0.005)]
        s = consistency_summary(rows)
        assert s["all_windows_positive"] is False
        assert s["n_positive_windows"] == 2
        assert s["frac_positive_of_usable"] == pytest.approx(0.4)

    def test_thin_sample_excluded_from_fractions(self) -> None:
        rows = [self._row(0.05), self._row(-0.5, thin=True), self._row(0.02)]
        s = consistency_summary(rows)
        assert s["n_usable_windows"] == 2
        assert s["n_positive_windows"] == 2
        # A thin sample also means "not all windows" for the strict all-positive check.
        assert s["all_windows_positive"] is False

    def test_recent_n_all_positive(self) -> None:
        rows = [self._row(-0.05), self._row(0.01), self._row(0.02), self._row(0.001), self._row(0.03)]
        s = consistency_summary(rows, recent_n=4)
        assert s["recent_all_positive"] is True  # last 4 all positive, only window 1 is negative

    def test_recent_n_not_all_positive(self) -> None:
        rows = [self._row(0.05), self._row(0.01), self._row(-0.02), self._row(0.001), self._row(0.03)]
        s = consistency_summary(rows, recent_n=4)
        assert s["recent_all_positive"] is False

    def test_recent_n_none_skips_check(self) -> None:
        rows = [self._row(0.05)]
        s = consistency_summary(rows, recent_n=None)
        assert s["recent_all_positive"] is None

    def test_means_by_window_preserves_order(self) -> None:
        rows = [self._row(0.01), self._row(-0.02), self._row(0.03)]
        s = consistency_summary(rows)
        assert s["means_by_window"] == [0.01, -0.02, 0.03]


class TestEntryQualityMask:
    def test_top_quartile(self) -> None:
        scores = np.arange(100)
        mask = entry_quality_mask(scores, top_quantile=0.75)
        assert mask.sum() == pytest.approx(25, abs=1)
        assert mask[-1] == True  # noqa: E712 -- highest score always in top quartile

    def test_top_decile_smaller_than_top_quartile(self) -> None:
        scores = np.arange(1000)
        decile_mask = entry_quality_mask(scores, top_quantile=0.90)
        quartile_mask = entry_quality_mask(scores, top_quantile=0.75)
        assert decile_mask.sum() < quartile_mask.sum()

    def test_empty_input(self) -> None:
        mask = entry_quality_mask([], top_quantile=0.75)
        assert len(mask) == 0

    def test_nan_never_qualifies(self) -> None:
        scores = [np.nan, np.nan, 1.0, 2.0, 3.0, 4.0]
        mask = entry_quality_mask(scores, top_quantile=0.5)
        assert not mask[0] and not mask[1]

    def test_all_nan_returns_all_false(self) -> None:
        mask = entry_quality_mask([np.nan, np.nan], top_quantile=0.5)
        assert not mask.any()


class TestDateConcentration:
    def test_broad_based_across_many_dates(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(1, 21) for _ in range(5)]  # 20 dates x 5 bars
        mask = np.ones(len(dates), dtype=bool)
        res = date_concentration(dates, mask)
        assert res["n_distinct_dates"] == 20
        assert res["top_date_share"] == pytest.approx(0.05)

    def test_concentrated_in_one_date_is_a_red_flag(self) -> None:
        dates = ["2026-01-01"] * 90 + [f"2026-02-{d:02d}" for d in range(1, 11)]  # 90 + 10
        mask = np.ones(len(dates), dtype=bool)
        res = date_concentration(dates, mask)
        assert res["n_distinct_dates"] == 11
        assert res["top_date_share"] == pytest.approx(0.9)
        assert res["top_date"] == "2026-01-01"

    def test_empty_mask(self) -> None:
        res = date_concentration(["2026-01-01", "2026-01-02"], [False, False])
        assert res["n_bars"] == 0
        assert res["n_distinct_dates"] == 0

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError):
            date_concentration(["2026-01-01"], [True, False])

    def test_top3_dates_share(self) -> None:
        dates = ["a"] * 5 + ["b"] * 5 + ["c"] * 5 + ["d"] * 85
        mask = np.ones(len(dates), dtype=bool)
        res = date_concentration(dates, mask)
        assert res["top3_dates_share"] == pytest.approx(0.95)  # d, plus two of a/b/c


class TestNearExpiryShare:
    def test_masked_share_matches_baseline_when_unrelated(self) -> None:
        rng = np.random.default_rng(0)
        n = 1000
        dte = rng.integers(0, 7, n).astype(float)
        mask = rng.uniform(0, 1, n) > 0.5  # unrelated to dte
        res = near_expiry_share(dte, mask, max_dte=0.0)
        assert res["baseline_share"] == pytest.approx(1 / 7, abs=0.03)
        assert res["masked_share"] == pytest.approx(res["baseline_share"], abs=0.05)

    def test_masked_share_much_higher_than_baseline_is_flagged(self) -> None:
        n = 200
        dte = np.array([0.0] * 20 + [3.0] * 180)  # 10% overall expiry-day bars
        mask = np.zeros(n, dtype=bool)
        mask[:20] = True  # the mask IS exactly the expiry-day bars
        res = near_expiry_share(dte, mask, max_dte=0.0)
        assert res["baseline_share"] == pytest.approx(0.10)
        assert res["masked_share"] == pytest.approx(1.0)

    def test_no_valid_masked_rows(self) -> None:
        dte = [np.nan, np.nan]
        mask = [True, True]
        res = near_expiry_share(dte, mask)
        assert res["masked_share"] is None
        assert res["n_masked_valid"] == 0

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError):
            near_expiry_share([0.0], [True, False])


class TestRegimeDiagnostics:
    def test_basic_stats(self) -> None:
        vix = [10.0, 11.0, 12.0, 13.0]
        adx = [20.0, 25.0, 30.0, 35.0]
        res = regime_diagnostics(vix, adx)
        assert res["vix_mean"] == pytest.approx(11.5)
        assert res["adx_mean"] == pytest.approx(27.5)
        assert res["n_vix"] == 4

    def test_low_std_flags_stable_regime(self) -> None:
        stable = regime_diagnostics([11.0, 11.1, 10.9, 11.0], [25.0, 25.0, 25.0, 25.0])
        volatile = regime_diagnostics([10.0, 15.0, 20.0, 12.0], [25.0, 25.0, 25.0, 25.0])
        assert stable["vix_std"] < volatile["vix_std"]

    def test_all_nan_returns_none_stats(self) -> None:
        res = regime_diagnostics([np.nan, np.nan], [np.nan, np.nan])
        assert res["vix_mean"] is None
        assert res["n_vix"] == 0

    def test_empty_input(self) -> None:
        res = regime_diagnostics([], [])
        assert res["vix_mean"] is None
        assert res["n_vix"] == 0
