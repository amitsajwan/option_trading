"""Tests for ml_pipeline_2.pipeline.meta_direction_features (the direction-
CALL-TRUST meta-labeling feature engineering -- part of the leftover
2026-09-26 stalled-session file set, verified 2026-09-27 to be sound,
instrument-agnostic, and free of the import-isolation problem
`ml_pipeline_2.pipeline.direction_replay` has; reused as-is, unmodified).

No real snapshot/parquet data required -- all fixtures are plain
dict/DataFrame rows, per this repo's synthetic-payload test convention (see
test_meta_entry_features.py / test_conditional_direction.py).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.meta_direction_features import (
    BARS_SINCE_ACTIVE_COL,
    COMPOSITE_OWN_INPUT_NAMES,
    ENTRY_QUALITY_SCORE_COL,
    META_DIRECTION_FEATURE_COLUMNS,
    SELECTED_REGIME_FEATURES,
    SELECTED_VELOCITY_FEATURES,
    bars_since_composite_active,
    build_meta_direction_features,
    build_meta_direction_features_df,
    select_regime_features,
    select_regime_features_df,
)


class TestNoLeakageOfCompositesOwnInputs:
    """The core anti-circularity guarantee this whole module exists to
    provide: a meta-model trained on META_DIRECTION_FEATURE_COLUMNS must
    never see any of composite's own raw scoring inputs, or it would just
    re-derive a recalibration of composite's own confidence under a new
    name (see the module's own docstring, "critical design constraint")."""

    def test_no_overlap_between_meta_features_and_composites_own_inputs(self) -> None:
        overlap = set(META_DIRECTION_FEATURE_COLUMNS) & COMPOSITE_OWN_INPUT_NAMES
        assert overlap == set()

    def test_composite_own_input_names_is_nonempty_sanity_check(self) -> None:
        # Guards against this test passing vacuously if COMPOSITE_OWN_INPUT_NAMES
        # were ever accidentally emptied.
        assert len(COMPOSITE_OWN_INPUT_NAMES) >= 10

    def test_meta_feature_columns_has_no_duplicates(self) -> None:
        assert len(META_DIRECTION_FEATURE_COLUMNS) == len(set(META_DIRECTION_FEATURE_COLUMNS))

    def test_meta_feature_columns_is_exactly_velocity_plus_regime_plus_streak_plus_entry_quality(self) -> None:
        expected = (
            list(SELECTED_VELOCITY_FEATURES)
            + list(SELECTED_REGIME_FEATURES)
            + [BARS_SINCE_ACTIVE_COL, ENTRY_QUALITY_SCORE_COL]
        )
        assert META_DIRECTION_FEATURE_COLUMNS == expected


class TestBarsSinceCompositeActive:
    """Thin wrapper over meta_entry_features.bars_since_primary_first_eligible
    -- re-verify the streak semantics under this module's own name/framing
    ("composite did not veto this bar")."""

    def test_all_active(self) -> None:
        assert bars_since_composite_active([True, True, True]) == [0, 1, 2]

    def test_streak_resets_after_a_veto(self) -> None:
        seq = [True, True, False, True, True, True]
        assert bars_since_composite_active(seq) == [0, 1, 0, 0, 1, 2]

    def test_empty_input(self) -> None:
        assert bars_since_composite_active([]) == []

    def test_none_active(self) -> None:
        assert bars_since_composite_active([False, False]) == [0, 0]


class TestSelectRegimeFeatures:
    def _futures_derived(self, **overrides: float) -> dict:
        row = {col: float(i + 1) for i, col in enumerate(SELECTED_REGIME_FEATURES)}
        # LEVEL/directional-lean columns this module deliberately excludes --
        # must NOT survive selection.
        row["ema_order"] = 1.0
        row["dist_from_ema21"] = 0.5
        row.update(overrides)
        return row

    def test_selects_exactly_the_selected_regime_columns(self) -> None:
        fd = self._futures_derived()
        out = select_regime_features(fd)
        assert set(out.keys()) == set(SELECTED_REGIME_FEATURES)
        assert "ema_order" not in out
        assert "dist_from_ema21" not in out

    def test_values_pass_through_unchanged(self) -> None:
        fd = self._futures_derived()
        out = select_regime_features(fd)
        for i, col in enumerate(SELECTED_REGIME_FEATURES):
            assert out[col] == pytest.approx(float(i + 1))

    def test_missing_column_degrades_to_nan_not_crash(self) -> None:
        fd = {"bb_width_20": 1.0}  # every other SELECTED_REGIME_FEATURES column absent
        out = select_regime_features(fd)
        assert out["bb_width_20"] == 1.0
        assert math.isnan(out["range_10"])

    def test_none_value_degrades_to_nan(self) -> None:
        fd = self._futures_derived(bb_width_20=None)
        out = select_regime_features(fd)
        assert math.isnan(out["bb_width_20"])

    def test_non_numeric_value_degrades_to_nan_not_raise(self) -> None:
        fd = self._futures_derived(bb_width_20="not_a_number")
        out = select_regime_features(fd)
        assert math.isnan(out["bb_width_20"])


class TestSelectRegimeFeaturesDf:
    def test_matches_single_row_version_across_multiple_rows(self) -> None:
        rows = [{col: float(i + j) for j, col in enumerate(SELECTED_REGIME_FEATURES)} for i in range(4)]
        df = pd.DataFrame(rows)
        out = select_regime_features_df(df)
        assert list(out.columns) == SELECTED_REGIME_FEATURES
        for col in SELECTED_REGIME_FEATURES:
            np.testing.assert_allclose(out[col].to_numpy(), df[col].to_numpy())

    def test_missing_columns_become_all_nan_columns(self) -> None:
        df = pd.DataFrame({"bb_width_20": [1.0, 2.0, 3.0]})
        out = select_regime_features_df(df)
        assert out["range_10"].isna().all()
        assert len(out) == 3


class TestBuildMetaDirectionFeatures:
    def _velocity_row(self) -> dict:
        return {col: float(i + 1) for i, col in enumerate(SELECTED_VELOCITY_FEATURES)}

    def _futures_derived(self) -> dict:
        return {col: float(i + 10) for i, col in enumerate(SELECTED_REGIME_FEATURES)}

    def test_returns_exactly_meta_direction_feature_columns(self) -> None:
        feats = build_meta_direction_features(
            self._velocity_row(), self._futures_derived(), bars_since_active=5, entry_quality_score=0.42,
        )
        assert set(feats.keys()) == set(META_DIRECTION_FEATURE_COLUMNS)

    def test_bars_since_active_and_entry_quality_score_pass_through(self) -> None:
        feats = build_meta_direction_features(
            self._velocity_row(), self._futures_derived(), bars_since_active=7, entry_quality_score=0.9,
        )
        assert feats[BARS_SINCE_ACTIVE_COL] == 7.0
        assert feats[ENTRY_QUALITY_SCORE_COL] == pytest.approx(0.9)

    def test_none_entry_quality_score_degrades_to_nan(self) -> None:
        feats = build_meta_direction_features(
            self._velocity_row(), self._futures_derived(), bars_since_active=0, entry_quality_score=None,
        )
        assert math.isnan(feats[ENTRY_QUALITY_SCORE_COL])


class TestBuildMetaDirectionFeaturesDf:
    def _frames(self, n: int = 5):
        velocity_df = pd.DataFrame(
            [{col: float(i) for col in SELECTED_VELOCITY_FEATURES} for i in range(n)]
        )
        regime_df = pd.DataFrame(
            [{col: float(i) for col in SELECTED_REGIME_FEATURES} for i in range(n)]
        )
        return velocity_df, regime_df

    def test_returns_meta_direction_feature_columns_in_order(self) -> None:
        velocity_df, regime_df = self._frames()
        out = build_meta_direction_features_df(
            velocity_df, regime_df, bars_since_active=[0, 1, 2, 3, 4], entry_quality_score=[0.1] * 5,
        )
        assert list(out.columns) == META_DIRECTION_FEATURE_COLUMNS
        assert len(out) == 5

    def test_mismatched_lengths_raise(self) -> None:
        velocity_df, regime_df = self._frames(n=5)
        with pytest.raises(ValueError, match="same length"):
            build_meta_direction_features_df(
                velocity_df, regime_df, bars_since_active=[0, 1], entry_quality_score=[0.1] * 5,
            )

    def test_entry_quality_score_column_values(self) -> None:
        velocity_df, regime_df = self._frames(n=3)
        scores = [0.1, 0.5, 0.9]
        out = build_meta_direction_features_df(
            velocity_df, regime_df, bars_since_active=[0, 0, 0], entry_quality_score=scores,
        )
        np.testing.assert_allclose(out[ENTRY_QUALITY_SCORE_COL].to_numpy(), scores)
