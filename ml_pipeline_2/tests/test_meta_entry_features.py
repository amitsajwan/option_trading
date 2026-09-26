"""Tests for the NIFTY entry-timing meta-labeling feature engineering
(ml_pipeline_2/src/ml_pipeline_2/pipeline/meta_entry_features.py) and its
data-prep script (ml_pipeline_2/scripts/train_meta_entry_v1.py).

No real Mongo/parquet/model data required -- all fixtures are plain
dict/DataFrame rows shaped like snapshot_app.core.velocity_features'
output columns, per this repo's synthetic-payload test convention (see
strategy_app/tests/test_entry_direction_resolver.py's `_snap()` helper and
ml_pipeline_2/tests/test_option_payoff_labels.py's fixture note).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.meta_entry_features import (
    BARS_SINCE_ELIGIBLE_COL,
    DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS,
    META_FEATURE_COLUMNS,
    PRIMARY_PROB_BUCKET_COL,
    SELECTED_VELOCITY_FEATURES,
    bars_since_primary_first_eligible,
    build_meta_features,
    build_meta_features_df,
    primary_probability_bucket,
    select_velocity_rate_features,
    select_velocity_rate_features_df,
)
from ml_pipeline_2.scripts.train_meta_entry_v1 import TARGET_COL, build_training_frame


def _velocity_row(**overrides: float) -> dict:
    """A minimal synthetic velocity-feature row -- one value per
    SELECTED_VELOCITY_FEATURES column plus a couple of LEVEL columns that
    must NOT survive selection (adx_14, ctx_am_trend), to prove the
    selector actually filters rather than passing everything through."""
    row = {col: float(i + 1) for i, col in enumerate(SELECTED_VELOCITY_FEATURES)}
    row["adx_14"] = 42.0
    row["ctx_am_trend"] = 1.0
    row.update(overrides)
    return row


class TestBarsSincePrimaryFirstEligible:
    def test_empty_input(self) -> None:
        assert bars_since_primary_first_eligible([]) == []

    def test_all_eligible(self) -> None:
        assert bars_since_primary_first_eligible([True, True, True]) == [0, 1, 2]

    def test_none_eligible(self) -> None:
        assert bars_since_primary_first_eligible([False, False, False]) == [0, 0, 0]

    def test_alternating(self) -> None:
        assert bars_since_primary_first_eligible([True, False, True, False]) == [0, 0, 0, 0]

    def test_streak_resets_after_a_gap(self) -> None:
        seq = [True, True, False, True, True, True]
        assert bars_since_primary_first_eligible(seq) == [0, 1, 0, 0, 1, 2]

    def test_streak_at_start_of_sequence(self) -> None:
        assert bars_since_primary_first_eligible([True, True, False]) == [0, 1, 0]

    def test_streak_at_end_of_sequence(self) -> None:
        assert bars_since_primary_first_eligible([False, True, True]) == [0, 0, 1]

    def test_accepts_truthy_ints_not_just_bools(self) -> None:
        assert bars_since_primary_first_eligible([1, 1, 0, 1]) == [0, 1, 0, 0]

    def test_single_element_sequences(self) -> None:
        assert bars_since_primary_first_eligible([True]) == [0]
        assert bars_since_primary_first_eligible([False]) == [0]


class TestPrimaryProbabilityBucket:
    def test_below_all_thresholds_is_bucket_zero(self) -> None:
        assert primary_probability_bucket(0.10) == 0

    def test_between_thresholds_is_bucket_one(self) -> None:
        lo, hi = DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS
        mid = (lo + hi) / 2.0
        assert primary_probability_bucket(mid) == 1

    def test_above_all_thresholds_is_top_bucket(self) -> None:
        assert primary_probability_bucket(0.99) == len(DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS)

    def test_exactly_at_a_threshold_counts_as_cleared(self) -> None:
        lo, _hi = DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS
        assert primary_probability_bucket(lo) == 1

    def test_nan_or_none_probability_is_bucket_zero_not_a_crash(self) -> None:
        assert primary_probability_bucket(float("nan")) == 0

    def test_custom_thresholds(self) -> None:
        assert primary_probability_bucket(0.5, thresholds=(0.3, 0.6, 0.9)) == 1
        assert primary_probability_bucket(0.95, thresholds=(0.3, 0.6, 0.9)) == 3


class TestSelectVelocityRateFeatures:
    def test_pulls_only_the_selected_columns(self) -> None:
        row = _velocity_row()
        out = select_velocity_rate_features(row)
        assert set(out.keys()) == set(SELECTED_VELOCITY_FEATURES)
        assert "adx_14" not in out
        assert "ctx_am_trend" not in out

    def test_values_pass_through_unchanged(self) -> None:
        row = _velocity_row(vel_ce_oi_build_rate=123.5)
        out = select_velocity_rate_features(row)
        assert out["vel_ce_oi_build_rate"] == 123.5

    def test_missing_column_becomes_nan_not_a_raise(self) -> None:
        row = {"vel_ce_oi_build_rate": 1.0}  # everything else missing
        out = select_velocity_rate_features(row)
        assert out["vel_ce_oi_build_rate"] == 1.0
        assert math.isnan(out["vel_pe_oi_build_rate"])

    def test_dataframe_variant_matches_row_variant(self) -> None:
        rows = [_velocity_row(vel_ce_oi_build_rate=float(i)) for i in range(3)]
        df = pd.DataFrame(rows)
        out = select_velocity_rate_features_df(df)
        assert list(out.columns) == SELECTED_VELOCITY_FEATURES
        assert out["vel_ce_oi_build_rate"].tolist() == [0.0, 1.0, 2.0]

    def test_dataframe_variant_missing_columns_become_nan_column(self) -> None:
        df = pd.DataFrame({"vel_ce_oi_build_rate": [1.0, 2.0]})
        out = select_velocity_rate_features_df(df)
        assert out["vel_pe_oi_build_rate"].isna().all()


class TestBuildMetaFeatures:
    def test_row_variant_emits_exactly_the_meta_feature_columns(self) -> None:
        row = _velocity_row()
        feats = build_meta_features(row, primary_probability=0.7, bars_since_first_eligible=3)
        assert set(feats.keys()) == set(META_FEATURE_COLUMNS)

    def test_row_variant_carries_streak_and_bucket_through(self) -> None:
        row = _velocity_row()
        feats = build_meta_features(row, primary_probability=0.85, bars_since_first_eligible=4)
        assert feats[BARS_SINCE_ELIGIBLE_COL] == 4.0
        assert feats[PRIMARY_PROB_BUCKET_COL] == float(len(DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS))

    def test_df_variant_shape_and_length_mismatch_raises(self) -> None:
        df = pd.DataFrame([_velocity_row()])
        with pytest.raises(ValueError):
            build_meta_features_df(df, primary_probability=[0.5, 0.6], bars_since_first_eligible=[0])

    def test_df_variant_emits_exactly_the_meta_feature_columns(self) -> None:
        df = pd.DataFrame([_velocity_row(), _velocity_row()])
        feats = build_meta_features_df(
            df, primary_probability=[0.5, 0.95], bars_since_first_eligible=[0, 5]
        )
        assert set(feats.columns) == set(META_FEATURE_COLUMNS)
        assert feats[BARS_SINCE_ELIGIBLE_COL].tolist() == [0.0, 5.0]

    def test_does_not_emit_a_raw_continuous_primary_probability_column(self) -> None:
        """The core anti-leakage constraint: only the coarse bucket may
        appear, never the raw probability value itself."""
        df = pd.DataFrame([_velocity_row(), _velocity_row()])
        raw_probs = [0.51, 0.93]
        feats = build_meta_features_df(df, primary_probability=raw_probs, bars_since_first_eligible=[0, 1])
        for col in feats.columns:
            assert "probability" not in col.lower(), f"leaked a probability-named column: {col}"
        # And the bucket column must actually be coarse (few distinct
        # small-integer values), not a re-hosting of the continuous input.
        bucket_vals = feats[PRIMARY_PROB_BUCKET_COL].tolist()
        assert bucket_vals != raw_probs
        assert all(v == int(v) for v in bucket_vals)
        assert all(0 <= v <= len(DEFAULT_PRIMARY_PROB_BUCKET_THRESHOLDS) for v in bucket_vals)


def _synthetic_bars(n: int) -> pd.DataFrame:
    """n chronological synthetic bars with velocity columns, an eligibility
    flag, a primary probability, and a payoff_score -- shaped like what
    train_meta_entry_v1.build_training_frame expects as --input-csv."""
    rows = []
    for i in range(n):
        row = _velocity_row(vel_ce_oi_build_rate=float(i))
        row["trade_date"] = "2026-09-01"
        row["timestamp"] = f"2026-09-01 09:{45 + i}:00"
        rows.append(row)
    return pd.DataFrame(rows)


class TestBuildTrainingFrame:
    def test_only_fired_bars_appear_in_output(self) -> None:
        df = _synthetic_bars(6)
        df["primary_eligible"] = [False, True, True, False, True, False]
        df["primary_probability"] = [0.1, 0.65, 0.90, 0.2, 0.55, 0.3]
        df["payoff_score"] = [0.0, 0.02, -0.01, 0.0, 0.03, 0.0]

        out = build_training_frame(df)
        assert len(out) == 3  # bars 1, 2, 4

    def test_binary_target_matches_payoff_score_sign(self) -> None:
        df = _synthetic_bars(4)
        df["primary_eligible"] = [True, True, True, True]
        df["primary_probability"] = [0.7, 0.7, 0.7, 0.7]
        df["payoff_score"] = [0.05, -0.02, 0.0, -0.0001]

        out = build_training_frame(df)
        # payoff_score > 0 -> True only for the first row (0.0 and -0.0001 are not > 0)
        assert out[TARGET_COL].tolist() == [1, 0, 0, 0]

    def test_rows_with_missing_payoff_are_dropped_not_imputed(self) -> None:
        df = _synthetic_bars(3)
        df["primary_eligible"] = [True, True, True]
        df["primary_probability"] = [0.7, 0.7, 0.7]
        df["payoff_score"] = [0.05, np.nan, -0.02]

        out = build_training_frame(df)
        assert len(out) == 2
        assert out[TARGET_COL].tolist() == [1, 0]

    def test_row_order_is_preserved_chronologically(self) -> None:
        df = _synthetic_bars(5)
        df["primary_eligible"] = [True, False, True, True, False]
        df["primary_probability"] = [0.6, 0.1, 0.6, 0.6, 0.1]
        df["payoff_score"] = [0.01, 0.0, 0.02, 0.03, 0.0]

        out = build_training_frame(df)
        assert out["timestamp"].tolist() == [df["timestamp"].iloc[0], df["timestamp"].iloc[2], df["timestamp"].iloc[3]]

    def test_streak_feature_computed_over_full_sequence_not_just_fired_rows(self) -> None:
        # eligible: T T F T T T  -> full-sequence streak: 0 1 0 0 1 2
        # fired bars are indices 0,1,3,4,5 with streak values 0,1,0,1,2
        df = _synthetic_bars(6)
        df["primary_eligible"] = [True, True, False, True, True, True]
        df["primary_probability"] = [0.6] * 6
        df["payoff_score"] = [0.01, 0.01, 0.0, 0.01, 0.01, 0.01]

        out = build_training_frame(df)
        assert out[BARS_SINCE_ELIGIBLE_COL].tolist() == [0.0, 1.0, 0.0, 1.0, 2.0]

    def test_output_columns_are_exactly_meta_features_plus_target_and_passthrough(self) -> None:
        df = _synthetic_bars(2)
        df["primary_eligible"] = [True, True]
        df["primary_probability"] = [0.6, 0.9]
        df["payoff_score"] = [0.01, -0.01]

        out = build_training_frame(df)
        expected = set(META_FEATURE_COLUMNS) | {TARGET_COL, "trade_date", "timestamp"}
        assert set(out.columns) == expected
        # explicit anti-leakage check at the script level too
        assert "primary_probability" not in out.columns
        assert "primary_eligible" not in out.columns
        assert "payoff_score" not in out.columns

    def test_missing_required_column_raises_clearly(self) -> None:
        df = _synthetic_bars(2)
        df["primary_eligible"] = [True, True]
        df["primary_probability"] = [0.6, 0.9]
        # no payoff_score column at all
        with pytest.raises(ValueError, match="payoff_score"):
            build_training_frame(df)

    def test_zero_fired_bars_returns_empty_frame_not_a_crash(self) -> None:
        df = _synthetic_bars(3)
        df["primary_eligible"] = [False, False, False]
        df["primary_probability"] = [0.1, 0.2, 0.3]
        df["payoff_score"] = [np.nan, np.nan, np.nan]

        out = build_training_frame(df)
        assert len(out) == 0
        assert set(out.columns) >= set(META_FEATURE_COLUMNS) | {TARGET_COL}
