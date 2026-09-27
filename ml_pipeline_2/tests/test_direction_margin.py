"""Tests for ml_pipeline_2.pipeline.direction_margin. Fixture convention
matches test_option_payoff_labels.py's `_snap()` helper: build a raw
payload dict, wrap in SnapshotAccessor."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.direction_margin import (
    CALL_ONLY_FEATURES,
    LEG_RAW_FEATURE_COLUMNS,
    PUT_ONLY_FEATURES,
    PromisingVerdict,
    build_leg_raw_frame,
    called_leg_actual_return,
    carve_inner_validation,
    compute_actual_margin,
    compute_predicted_margin,
    extract_leg_raw_row,
    is_promising,
    margin_to_side,
    select_confident_subset_mask,
)
from strategy_app.market.snapshot_accessor import SnapshotAccessor

LOT_SIZE = 65


def _snap(
    *,
    timestamp: str = "2026-01-05T04:15:00+00:00",  # 09:45 IST
    trade_date: str = "2026-01-05",
    atm_ce_iv: float | None = 14.0,
    atm_pe_iv: float | None = 15.0,
    atm_ce_volume: float | None = 12000.0,
    atm_pe_volume: float | None = 9000.0,
    atm_ce_oi: float | None = 50000.0,
    atm_pe_oi: float | None = 48000.0,
    atm_ce_oi_change_30m: float | None = 500.0,
    atm_pe_oi_change_30m: float | None = -300.0,
    atm_ce_vol_ratio: float | None = 1.2,
    atm_pe_vol_ratio: float | None = 0.9,
) -> SnapshotAccessor:
    payload: dict = {
        "snapshot_id": "s1",
        "timestamp": timestamp,
        "trade_date": trade_date,
        "session_context": {"date": trade_date, "time": timestamp},
        "atm_options": {
            "atm_ce_iv": atm_ce_iv, "atm_pe_iv": atm_pe_iv,
            "atm_ce_volume": atm_ce_volume, "atm_pe_volume": atm_pe_volume,
            "atm_ce_oi": atm_ce_oi, "atm_pe_oi": atm_pe_oi,
            "atm_ce_oi_change_30m": atm_ce_oi_change_30m, "atm_pe_oi_change_30m": atm_pe_oi_change_30m,
            "atm_ce_vol_ratio": atm_ce_vol_ratio, "atm_pe_vol_ratio": atm_pe_vol_ratio,
        },
    }
    return SnapshotAccessor(payload)


class TestExtractLegRawRow:
    def test_pulls_every_leg_raw_column(self) -> None:
        snap = _snap()
        row = extract_leg_raw_row(snap, bar_index=7)
        assert row["trade_date"] == "2026-01-05"
        assert row["bar_index"] == 7
        for col in LEG_RAW_FEATURE_COLUMNS:
            assert col in row
        assert row["atm_ce_iv"] == 14.0
        assert row["atm_pe_iv"] == 15.0
        assert row["atm_ce_oi_change_30m"] == 500.0
        assert row["atm_pe_vol_ratio"] == 0.9

    def test_missing_field_is_none_not_raised(self) -> None:
        snap = _snap(atm_ce_oi=None)
        row = extract_leg_raw_row(snap, bar_index=0)
        assert row["atm_ce_oi"] is None


class TestBuildLegRawFrame:
    def test_one_row_per_snapshot_in_order(self) -> None:
        snaps = [_snap(timestamp=f"2026-01-05T0{4+i}:00:00+00:00") for i in range(3)]
        df = build_leg_raw_frame(snaps)
        assert len(df) == 3
        assert list(df["bar_index"]) == [0, 1, 2]
        assert set(LEG_RAW_FEATURE_COLUMNS).issubset(df.columns)

    def test_empty_input_returns_empty_frame_with_right_columns(self) -> None:
        df = build_leg_raw_frame([])
        assert len(df) == 0
        assert "atm_ce_iv" in df.columns
        assert "bar_index" in df.columns


class TestFeatureLists:
    def test_call_and_put_only_features_are_disjoint(self) -> None:
        assert set(CALL_ONLY_FEATURES).isdisjoint(set(PUT_ONLY_FEATURES))

    def test_call_features_reference_only_ce_fields(self) -> None:
        for f in CALL_ONLY_FEATURES:
            assert "_pe_" not in f, f"{f} looks PE-specific but is in CALL_ONLY_FEATURES"

    def test_put_features_reference_only_pe_fields(self) -> None:
        for f in PUT_ONLY_FEATURES:
            assert "_ce_" not in f, f"{f} looks CE-specific but is in PUT_ONLY_FEATURES"


class TestComputeActualMargin:
    def test_positive_when_ce_outperforms(self) -> None:
        out = compute_actual_margin([0.10], [0.02])
        assert out[0] == pytest.approx(0.08)

    def test_negative_when_pe_outperforms(self) -> None:
        out = compute_actual_margin([0.02], [0.10])
        assert out[0] == pytest.approx(-0.08)

    def test_nan_when_either_side_missing(self) -> None:
        out = compute_actual_margin([0.10, None, 0.05], [None, 0.02, 0.01])
        assert math.isnan(out[0])
        assert math.isnan(out[1])
        assert out[2] == pytest.approx(0.04)


class TestComputePredictedMargin:
    def test_simple_difference(self) -> None:
        out = compute_predicted_margin([0.05, -0.01], [0.02, 0.03])
        assert out[0] == pytest.approx(0.03)
        assert out[1] == pytest.approx(-0.04)

    def test_raises_on_length_mismatch(self) -> None:
        with pytest.raises(ValueError):
            compute_predicted_margin([0.1, 0.2], [0.1])


class TestMarginToSide:
    def test_positive_is_ce_negative_is_pe(self) -> None:
        out = margin_to_side([0.05, -0.05, 0.0, np.nan])
        assert list(out) == ["CE", "PE", None, None]

    def test_epsilon_veto_band(self) -> None:
        out = margin_to_side([0.005, -0.005, 0.05], epsilon=0.01)
        assert list(out) == [None, None, "CE"]


class TestCalledLegActualReturn:
    def test_picks_ce_leg_when_called_ce(self) -> None:
        out = called_leg_actual_return([0.10, 0.20], [0.01, -0.05], ["CE", "CE"])
        assert out[0] == pytest.approx(0.10)
        assert out[1] == pytest.approx(0.20)

    def test_picks_pe_leg_when_called_pe(self) -> None:
        out = called_leg_actual_return([0.10, 0.20], [0.01, -0.05], ["PE", "PE"])
        assert out[0] == pytest.approx(0.01)
        assert out[1] == pytest.approx(-0.05)

    def test_none_call_is_nan(self) -> None:
        out = called_leg_actual_return([0.10], [0.01], [None])
        assert math.isnan(out[0])

    def test_can_be_the_losing_side(self) -> None:
        # Called CE, but PE was actually the leg that would have paid --
        # must report the CALLED (CE) return, not max(ce, pe).
        out = called_leg_actual_return([-0.10], [0.30], ["CE"])
        assert out[0] == pytest.approx(-0.10)

    def test_raises_on_length_mismatch(self) -> None:
        with pytest.raises(ValueError):
            called_leg_actual_return([0.1, 0.2], [0.1], ["CE", "CE"])


class TestCarveInnerValidation:
    def test_splits_by_date_fraction(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(1, 21)]  # 20 days
        train_end, valid_start = carve_inner_validation(dates, valid_frac=0.2, min_valid_days=2)
        # 20 * 0.2 = 4 valid days -> last 4 days are validation
        assert train_end == "2026-01-16"
        assert valid_start == "2026-01-17"

    def test_enforces_min_valid_days_floor(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(1, 21)]
        train_end, valid_start = carve_inner_validation(dates, valid_frac=0.01, min_valid_days=5)
        assert valid_start == "2026-01-16"  # 5 days reserved, not 20*0.01=0.2->1

    def test_raises_when_too_few_days(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(1, 6)]  # 5 days
        with pytest.raises(ValueError):
            carve_inner_validation(dates, valid_frac=0.5, min_valid_days=5)


class TestSelectConfidentSubsetMask:
    def test_selects_both_tails(self) -> None:
        margin = list(range(100))  # 0..99, monotonically increasing
        mask = select_confident_subset_mask(margin, frac=0.1)
        assert mask.sum() == 20  # 10 lowest + 10 highest
        selected = np.asarray(margin)[mask]
        assert set(selected[:10]) == set(range(10))
        assert set(selected[10:]) == set(range(90, 100))

    def test_empty_input(self) -> None:
        mask = select_confident_subset_mask([], frac=0.1)
        assert len(mask) == 0

    def test_at_least_one_per_tail(self) -> None:
        margin = [1.0, 2.0, 3.0, 4.0, 5.0]
        mask = select_confident_subset_mask(margin, frac=0.01)
        assert mask.sum() >= 2  # at least one from each tail (k=max(1,...))


class TestIsPromising:
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            pooled_margin_spearman_rho=0.0,
            pooled_margin_spearman_p=1.0,
            pooled_top_decile_mean_actual_margin=None,
            pooled_bottom_decile_mean_actual_margin=None,
            pooled_binary_accuracy=0.50,
            pooled_binary_permutation_p=0.90,
            per_window_binary_accuracy=[0.49, 0.51, 0.50, 0.50, 0.50],
        )
        kwargs.update(overrides)
        return kwargs

    def test_null_result_is_not_promising(self) -> None:
        verdict = is_promising(**self._base_kwargs())
        assert isinstance(verdict, PromisingVerdict)
        assert verdict.promising is False
        assert verdict.reasons == []

    def test_real_spearman_is_promising(self) -> None:
        verdict = is_promising(**self._base_kwargs(pooled_margin_spearman_rho=0.10, pooled_margin_spearman_p=0.001))
        assert verdict.promising is True
        assert any("Spearman" in r for r in verdict.reasons)

    def test_correctly_signed_decile_spread_is_promising(self) -> None:
        verdict = is_promising(**self._base_kwargs(
            pooled_top_decile_mean_actual_margin=0.05, pooled_bottom_decile_mean_actual_margin=-0.04,
        ))
        assert verdict.promising is True

    def test_wrongly_signed_decile_spread_is_not_promising(self) -> None:
        # Top decile (most CE-favoring) actually LOSES for CE on average --
        # inverted, should not count as promising.
        verdict = is_promising(**self._base_kwargs(
            pooled_top_decile_mean_actual_margin=-0.05, pooled_bottom_decile_mean_actual_margin=0.04,
        ))
        assert verdict.promising is False

    def test_binary_accuracy_needs_walk_forward_consistency(self) -> None:
        # Pooled accuracy clears the bar, but only one window is above chance
        # -- driven by an outlier window, should NOT count as promising.
        verdict = is_promising(**self._base_kwargs(
            pooled_binary_accuracy=0.60, pooled_binary_permutation_p=0.01,
            per_window_binary_accuracy=[0.80, 0.40, 0.45, 0.48, 0.47],
        ))
        assert verdict.promising is False

    def test_binary_accuracy_promising_when_consistent(self) -> None:
        verdict = is_promising(**self._base_kwargs(
            pooled_binary_accuracy=0.60, pooled_binary_permutation_p=0.01,
            per_window_binary_accuracy=[0.58, 0.61, 0.55, 0.62, 0.59],
        ))
        assert verdict.promising is True
