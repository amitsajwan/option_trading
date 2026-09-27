"""Tests for ml_pipeline_2.pipeline.direction_trust_meta (the direction-
CALL-TRUST meta-labeling modeling/statistics layer). Fixture convention
matches test_entry_quality_direction.py's `_snap()` helper for anything
touching SnapshotAccessor; everything else uses plain synthetic
DataFrames/arrays, per test_conditional_direction.py's convention.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.conditional_direction import REQUIRED_RAW_COLUMNS
from ml_pipeline_2.pipeline.direction_trust_meta import (
    CE,
    PE,
    add_bars_since_active,
    assemble_meta_feature_frame,
    bootstrap_lift_ci,
    compute_composite_calls,
    compute_composite_correct,
    evaluate_trust_filter,
    fit_entry_quality_score_walkforward,
    fit_trust_meta_model,
    permutation_test_lift,
    predict_trust_score,
    recompute_composite_correct_at_delay,
    summarize_delayed_trust_filter,
    trusted_mask_per_group,
)
from ml_pipeline_2.pipeline.entry_quality_direction import compute_payoff_at_delay
from ml_pipeline_2.pipeline.meta_direction_features import (
    BARS_SINCE_ACTIVE_COL,
    ENTRY_QUALITY_SCORE_COL,
    META_DIRECTION_FEATURE_COLUMNS,
    SELECTED_REGIME_FEATURES,
    SELECTED_VELOCITY_FEATURES,
)
from strategy_app.market.snapshot_accessor import SnapshotAccessor


def _base_df(n: int, *, trade_date: str = "2026-01-05") -> pd.DataFrame:
    df = pd.DataFrame({col: [0.0] * n for col in REQUIRED_RAW_COLUMNS})
    df["trade_date"] = trade_date
    df["bar_index"] = range(n)
    return df


class TestComputeCompositeCalls:
    def test_adds_direction_and_fired_columns(self) -> None:
        df = _base_df(3)
        df["fut_return_5m"] = [2.0, -2.0, 0.0]
        df["fut_return_15m"] = [2.0, -2.0, 0.0]
        df["price_vs_vwap"] = [0.5, -0.5, 0.0]
        out = compute_composite_calls(df)
        assert list(out["composite_direction"]) == [CE, PE, None]
        assert list(out["composite_fired"]) == [True, True, False]

    def test_does_not_mutate_input_df(self) -> None:
        df = _base_df(1)
        df["fut_return_5m"] = [2.0]
        _ = compute_composite_calls(df)
        assert "composite_direction" not in df.columns


class TestComputeCompositeCorrect:
    def test_fired_and_matching_is_one(self) -> None:
        direction = pd.Series([CE, PE, None])
        truth = pd.Series([CE, CE, CE])
        out = compute_composite_correct(direction, truth)
        assert out.iloc[0] == 1.0
        assert out.iloc[1] == 0.0
        assert np.isnan(out.iloc[2])  # abstained -- NaN, not 0

    def test_missing_truth_is_nan_not_zero(self) -> None:
        direction = pd.Series([CE])
        truth = pd.Series([None])
        out = compute_composite_correct(direction, truth)
        assert np.isnan(out.iloc[0])


class TestAddBarsSinceActive:
    def test_streak_resets_at_trade_date_boundary(self) -> None:
        # Day 1: fired, fired -> streak [0, 1]. Day 2 starts fresh even
        # though day 1 ended on an active streak.
        df = pd.concat(
            [
                pd.DataFrame({"trade_date": "2026-01-05", "composite_fired": [True, True]}),
                pd.DataFrame({"trade_date": "2026-01-06", "composite_fired": [True, False, True]}),
            ],
            ignore_index=True,
        )
        out = add_bars_since_active(df)
        assert list(out) == [0, 1, 0, 0, 0]

    def test_handles_non_default_index(self) -> None:
        df = pd.DataFrame({"trade_date": ["2026-01-05"] * 3, "composite_fired": [True, True, True]})
        df.index = [10, 11, 12]
        out = add_bars_since_active(df)
        assert list(out) == [0, 1, 2]
        assert list(out.index) == [10, 11, 12]


class TestFitEntryQualityScoreWalkforward:
    xgb = pytest.importorskip("xgboost")

    def _synthetic(self, n: int, seed: int) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        f1 = rng.normal(size=n)
        f2 = rng.normal(size=n)
        payoff = 0.05 + 0.03 * f1 + rng.normal(scale=0.01, size=n)
        return pd.DataFrame({"f1": f1, "f2": f2, "payoff_score": payoff})

    def test_returns_oof_and_test_scores_with_correct_shapes(self) -> None:
        train_df = self._synthetic(300, seed=0)
        test_df = self._synthetic(100, seed=1)
        train_oof, test_scores = fit_entry_quality_score_walkforward(train_df, test_df, ["f1", "f2"])
        assert len(train_oof) == len(train_df)
        assert len(test_scores) == len(test_df)
        assert np.isfinite(train_oof).all()
        assert np.isfinite(test_scores).all()

    def test_test_scores_correlate_with_the_real_signal(self) -> None:
        train_df = self._synthetic(400, seed=2)
        test_df = self._synthetic(200, seed=3)
        _, test_scores = fit_entry_quality_score_walkforward(train_df, test_df, ["f1", "f2"])
        from scipy.stats import spearmanr
        rho, _ = spearmanr(test_scores, test_df["payoff_score"])
        assert rho > 0.3


class TestAssembleMetaFeatureFrame:
    def _df_with_required_cols(self, n: int) -> pd.DataFrame:
        cols = list(SELECTED_VELOCITY_FEATURES) + list(SELECTED_REGIME_FEATURES) + [BARS_SINCE_ACTIVE_COL]
        return pd.DataFrame({c: np.arange(n, dtype=float) for c in cols})

    def test_returns_exactly_meta_direction_feature_columns(self) -> None:
        df = self._df_with_required_cols(5)
        out = assemble_meta_feature_frame(df, entry_quality_score=[0.1, 0.2, 0.3, 0.4, 0.5])
        assert list(out.columns) == META_DIRECTION_FEATURE_COLUMNS
        assert len(out) == 5

    def test_entry_quality_score_column_matches_input(self) -> None:
        df = self._df_with_required_cols(3)
        scores = [0.9, 0.1, 0.5]
        out = assemble_meta_feature_frame(df, entry_quality_score=scores)
        np.testing.assert_allclose(out[ENTRY_QUALITY_SCORE_COL].to_numpy(), scores)

    def test_length_mismatch_raises(self) -> None:
        df = self._df_with_required_cols(5)
        with pytest.raises(ValueError, match="length"):
            assemble_meta_feature_frame(df, entry_quality_score=[0.1, 0.2])

    def test_missing_required_column_raises(self) -> None:
        df = self._df_with_required_cols(3).drop(columns=["vel_ce_oi_build_rate"])
        with pytest.raises(ValueError, match="missing required"):
            assemble_meta_feature_frame(df, entry_quality_score=[0.1, 0.2, 0.3])


class TestFitTrustMetaModel:
    xgb = pytest.importorskip("xgboost")

    def test_learns_a_real_separable_signal(self) -> None:
        rng = np.random.default_rng(5)
        n = 400
        f1 = rng.normal(size=n)
        y = (f1 > 0).astype(int)
        X = pd.DataFrame({"f1": f1, "noise": rng.normal(size=n)})
        model, medians = fit_trust_meta_model(X, y)
        preds = predict_trust_score(model, medians, X)
        assert ((preds > 0.5).astype(int) == y).mean() > 0.85

    def test_predict_handles_nan_features(self) -> None:
        rng = np.random.default_rng(6)
        n = 200
        f1 = rng.normal(size=n)
        y = (f1 > 0).astype(int)
        X = pd.DataFrame({"f1": f1})
        model, medians = fit_trust_meta_model(X, y)
        X_holdout = X.copy()
        X_holdout.loc[0, "f1"] = np.nan
        preds = predict_trust_score(model, medians, X_holdout)
        assert preds.shape[0] == n
        assert np.isfinite(preds).all()


class TestBootstrapAndPermutation:
    def test_perfect_trust_score_gives_full_accuracy_when_filtered(self) -> None:
        rng = np.random.default_rng(7)
        n = 1000
        y = rng.integers(0, 2, size=n).astype(float)
        p = y.copy()  # perfect meta-model: trust score == truth
        result = evaluate_trust_filter(y, p, frac=0.25)
        assert result["filtered_accuracy"] == 1.0
        assert result["lift"] > 0
        assert result["perm_p"] < 0.01

    def test_unrelated_trust_score_gives_near_zero_lift_and_high_p(self) -> None:
        rng = np.random.default_rng(8)
        n = 1000
        y = rng.integers(0, 2, size=n).astype(float)
        p = rng.random(n)  # independent of y
        result = evaluate_trust_filter(y, p, frac=0.5, n_boot=500, n_perm=500)
        assert abs(result["lift"]) < 0.1
        assert result["perm_p"] > 0.05

    def test_bootstrap_ci_is_ordered(self) -> None:
        rng = np.random.default_rng(9)
        n = 500
        y = rng.integers(0, 2, size=n).astype(float)
        p = y + rng.normal(scale=0.3, size=n)
        lo, hi = bootstrap_lift_ci(y, p, frac=0.3, n_boot=500)
        assert lo <= hi

    def test_empty_input_does_not_crash(self) -> None:
        result = evaluate_trust_filter(np.array([]), np.array([]), frac=0.25)
        assert result["n_filtered"] == 0
        assert np.isnan(result["filtered_accuracy"])


class TestTrustedMaskPerGroup:
    def test_selects_top_fraction_within_each_group_independently(self) -> None:
        window_ids = [1, 1, 1, 1, 2, 2, 2, 2]
        scores = [0.1, 0.2, 0.3, 0.4, 0.9, 0.8, 0.1, 0.2]  # group2's scale is unrelated to group1's
        mask = trusted_mask_per_group(window_ids, scores, frac=0.5)
        # top half of group 1 (scores 0.3, 0.4) and top half of group 2 (0.9, 0.8)
        assert list(mask) == [False, False, True, True, True, True, False, False]

    def test_frac_one_selects_everything(self) -> None:
        mask = trusted_mask_per_group([1, 1, 1], [0.1, 0.5, 0.9], frac=1.0)
        assert mask.all()

    def test_top_fraction_is_a_subset_as_frac_shrinks(self) -> None:
        window_ids = [1] * 20
        rng = np.random.default_rng(1)
        scores = rng.random(20)
        mask_50 = trusted_mask_per_group(window_ids, scores, frac=0.5)
        mask_25 = trusted_mask_per_group(window_ids, scores, frac=0.25)
        assert set(np.flatnonzero(mask_25)).issubset(set(np.flatnonzero(mask_50)))


class TestSummarizeDelayedTrustFilter:
    def test_thin_sample_reported_when_too_few_valid_bars(self) -> None:
        mask = np.array([True] * 5)
        result = summarize_delayed_trust_filter([1.0] * 5, [1.0] * 5, [1.0] * 5, mask, min_valid_bars=20)
        assert result["status"] == "thin_sample"

    def test_no_edge_at_fire_reported_when_below_threshold(self) -> None:
        n = 30
        correct0 = [1.0, 0.0] * (n // 2)  # 50% accuracy -- no edge
        mask = np.array([True] * n)
        result = summarize_delayed_trust_filter(correct0, correct0, correct0, mask, min_valid_bars=20, edge_threshold=0.55)
        assert result["status"] == "no_edge_at_fire"

    def test_edge_that_collapses_at_delay_is_flagged(self) -> None:
        n = 40
        correct0 = [1.0] * 36 + [0.0] * 4         # 90% at fire
        correct1 = [1.0] * 18 + [0.0] * 22        # collapses to <=50%
        correct2 = [1.0] * 16 + [0.0] * 24
        mask = np.array([True] * n)
        result = summarize_delayed_trust_filter(correct0, correct1, correct2, mask, min_valid_bars=20, edge_threshold=0.55)
        assert result["status"] == "collapsed"

    def test_edge_that_survives_delay_is_flagged(self) -> None:
        n = 40
        correct0 = [1.0] * 36 + [0.0] * 4
        correct1 = [1.0] * 34 + [0.0] * 6
        correct2 = [1.0] * 33 + [0.0] * 7
        mask = np.array([True] * n)
        result = summarize_delayed_trust_filter(correct0, correct1, correct2, mask, min_valid_bars=20, edge_threshold=0.55)
        assert result["status"] == "survived"

    def test_nan_rows_excluded_from_valid_count(self) -> None:
        n = 30
        correct0 = [1.0] * n
        correct1 = [1.0] * (n - 5) + [np.nan] * 5
        correct2 = [1.0] * n
        mask = np.array([True] * n)
        result = summarize_delayed_trust_filter(correct0, correct1, correct2, mask, min_valid_bars=20, edge_threshold=0.55)
        assert result["n_valid"] == n - 5

    def test_untrusted_rows_excluded_by_mask(self) -> None:
        n = 40
        correct0 = [1.0] * n
        correct1 = [1.0] * n
        correct2 = [1.0] * n
        mask = np.array([True] * 10 + [False] * 30)
        result = summarize_delayed_trust_filter(correct0, correct1, correct2, mask, min_valid_bars=5, edge_threshold=0.55)
        assert result["n_valid"] == 10


def _snap(
    *,
    timestamp: str,
    atm_strike: int = 24500,
    atm_ce_close: float = 100.0,
    atm_pe_close: float = 100.0,
    strikes: list | None = None,
) -> SnapshotAccessor:
    payload = {
        "snapshot_id": "s1",
        "timestamp": timestamp,
        "trade_date": timestamp[:10],
        "session_context": {"date": timestamp[:10], "time": timestamp},
        "chain_aggregates": {"atm_strike": atm_strike},
        "atm_options": {
            "atm_ce_close": atm_ce_close, "atm_pe_close": atm_pe_close,
            "atm_ce_oi": 50000.0, "atm_pe_oi": 50000.0,
        },
        "futures_derived": {},
        "futures_bar": {"fut_close": 24550.0},
        "opening_range": {},
        "vix_context": {},
        "strikes": strikes or [],
    }
    return SnapshotAccessor(payload)


class TestRecomputeCompositeCorrectAtDelay:
    def _rallying_day(self, trade_date: str, n_bars: int = 20) -> list[SnapshotAccessor]:
        out = []
        for i in range(n_bars):
            ts = pd.Timestamp(f"{trade_date} 04:15:00") + pd.Timedelta(minutes=i)
            ce = 100.0 + i * 2.0  # CE always wins -- rallying day
            pe = max(5.0, 100.0 - i * 2.0)
            out.append(_snap(
                timestamp=ts.isoformat() + "+00:00", atm_ce_close=ce, atm_pe_close=pe,
                strikes=[{"strike": 24500, "ce_ltp": ce, "pe_ltp": pe}],
            ))
        return out

    def test_recomputes_correctness_using_delayed_entry_same_exit(self) -> None:
        snaps = self._rallying_day("2026-01-05")

        def fake_read(parquet_base: str, trade_date: str):
            return snaps if trade_date == "2026-01-05" else []

        rows = pd.DataFrame({"trade_date": ["2026-01-05"], "bar_index": [0], "composite_direction": [CE]})
        out = recompute_composite_correct_at_delay(
            rows, "unused", delay_bars=2, horizon_bars=10, lot_size=65, read_day_snapshots_fn=fake_read,
        )
        # CE always wins on a rallying day -- frozen call CE should be correct.
        assert out.iloc[0] == 1.0

    def test_missing_day_data_yields_nan(self) -> None:
        def fake_read(parquet_base: str, trade_date: str):
            return []

        rows = pd.DataFrame({"trade_date": ["2026-02-02"], "bar_index": [0], "composite_direction": [CE]})
        out = recompute_composite_correct_at_delay(
            rows, "unused", delay_bars=1, horizon_bars=5, lot_size=65, read_day_snapshots_fn=fake_read,
        )
        assert np.isnan(out.iloc[0])

    def test_wrong_frozen_call_scores_zero(self) -> None:
        snaps = self._rallying_day("2026-01-05")

        def fake_read(parquet_base: str, trade_date: str):
            return snaps

        rows = pd.DataFrame({"trade_date": ["2026-01-05"], "bar_index": [0], "composite_direction": [PE]})
        out = recompute_composite_correct_at_delay(
            rows, "unused", delay_bars=1, horizon_bars=10, lot_size=65, read_day_snapshots_fn=fake_read,
        )
        assert out.iloc[0] == 0.0

    def test_multiple_rows_grouped_by_trade_date_read_once_per_day(self) -> None:
        snaps = self._rallying_day("2026-01-05")
        call_count = {"n": 0}

        def fake_read(parquet_base: str, trade_date: str):
            call_count["n"] += 1
            return snaps

        rows = pd.DataFrame(
            {"trade_date": ["2026-01-05", "2026-01-05"], "bar_index": [0, 1], "composite_direction": [CE, CE]}
        )
        out = recompute_composite_correct_at_delay(
            rows, "unused", delay_bars=1, horizon_bars=10, lot_size=65, read_day_snapshots_fn=fake_read,
        )
        assert call_count["n"] == 1  # one groupby group -> one read, not one per row
        assert len(out) == 2

    def test_delay_beyond_exit_bar_yields_nan_matching_compute_payoff_at_delay(self) -> None:
        snaps = self._rallying_day("2026-01-05", n_bars=10)

        def fake_read(parquet_base: str, trade_date: str):
            return snaps

        rows = pd.DataFrame({"trade_date": ["2026-01-05"], "bar_index": [0], "composite_direction": [CE]})
        out = recompute_composite_correct_at_delay(
            rows, "unused", delay_bars=5, horizon_bars=2, lot_size=65, read_day_snapshots_fn=fake_read,
        )
        assert np.isnan(out.iloc[0])
        # sanity: the underlying primitive agrees this combination is unresolvable
        assert compute_payoff_at_delay(snaps, 0, horizon_bars=2, delay_bars=5, lot_size=65) is None
