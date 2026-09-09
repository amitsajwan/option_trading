from __future__ import annotations

import pandas as pd
import pytest

from ml_pipeline_2.pipeline.windowing import apply_event_overlap_purge, build_day_folds, max_drawdown_pct


class TestBuildDayFolds:
    def test_basic_fold_shape_no_gaps(self) -> None:
        days = [f"d{i:02d}" for i in range(10)]
        folds = build_day_folds(days, train_days=4, valid_days=2, test_days=2, step_days=8)
        assert len(folds) == 1
        fold = folds[0]
        assert fold["train_days"] == ["d00", "d01", "d02", "d03"]
        assert fold["valid_days"] == ["d04", "d05"]
        assert fold["test_days"] == ["d06", "d07"]
        assert fold["purge_days"] == []
        assert fold["embargo_days"] == []

    def test_embargo_gap_actually_separates_valid_and_test(self) -> None:
        # This is the walk-forward equivalent of the fix for the in-sample
        # contamination incident: the embargo days exist purely so test
        # never starts immediately after valid ends.
        days = [f"d{i:02d}" for i in range(12)]
        folds = build_day_folds(days, train_days=4, valid_days=2, test_days=2, step_days=12, embargo_days=3)
        fold = folds[0]
        assert fold["embargo_days"] == ["d06", "d07", "d08"]
        assert fold["test_days"] == ["d09", "d10"]
        # No day appears in both valid and test.
        assert not (set(fold["valid_days"]) & set(fold["test_days"]))

    def test_purge_gap_separates_train_and_valid(self) -> None:
        days = [f"d{i:02d}" for i in range(10)]
        folds = build_day_folds(days, train_days=4, valid_days=2, test_days=2, step_days=10, purge_days=2)
        fold = folds[0]
        assert fold["purge_days"] == ["d04", "d05"]
        assert fold["valid_days"] == ["d06", "d07"]

    def test_stepping_produces_multiple_folds(self) -> None:
        days = [f"d{i:02d}" for i in range(20)]
        folds = build_day_folds(days, train_days=4, valid_days=2, test_days=2, step_days=4)
        assert len(folds) > 1
        # Each fold's train window starts step_days later than the last.
        assert folds[1]["train_days"][0] == "d04"

    def test_not_enough_days_produces_zero_folds(self) -> None:
        days = ["d00", "d01", "d02"]
        folds = build_day_folds(days, train_days=4, valid_days=2, test_days=2, step_days=8)
        assert folds == []

    def test_rejects_non_positive_segment_sizes(self) -> None:
        with pytest.raises(ValueError):
            build_day_folds(["d00"], train_days=0, valid_days=1, test_days=1, step_days=1)

    def test_rejects_negative_gaps(self) -> None:
        with pytest.raises(ValueError):
            build_day_folds(["d00"], train_days=1, valid_days=1, test_days=1, step_days=1, embargo_days=-1)


class TestMaxDrawdownPct:
    def test_empty_returns_zero(self) -> None:
        assert max_drawdown_pct([]) == 0.0

    def test_all_gains_has_zero_drawdown(self) -> None:
        assert max_drawdown_pct([0.01, 0.02, 0.01]) == pytest.approx(0.0)

    def test_a_loss_after_gains_produces_the_peak_to_trough_drawdown(self) -> None:
        # +10%, +10%, then a -20% trade: drawdown measured from the peak
        # (1.10*1.10 = 1.21) down through the loss (1.21*0.80 = 0.968),
        # not from the starting point.
        equity_path = [0.10, 0.10, -0.20]
        dd = max_drawdown_pct(equity_path)
        assert dd == pytest.approx(1.0 - (0.968 / 1.21), rel=1e-6)

    def test_flat_start_then_a_loss_gives_the_loss_size_as_drawdown(self) -> None:
        # A single-point series has no prior peak to fall FROM (the one
        # point is its own peak) -- max_drawdown_pct([-0.15]) is correctly
        # 0.0, not 0.15. A flat starting point establishes a real peak
        # first, so the subsequent loss actually registers.
        assert max_drawdown_pct([0.0, -0.15]) == pytest.approx(0.15)

    def test_single_point_series_has_no_prior_peak_to_fall_from(self) -> None:
        assert max_drawdown_pct([-0.15]) == pytest.approx(0.0)


class TestApplyEventOverlapPurge:
    def test_drops_a_training_row_whose_event_window_overlaps_holdout(self) -> None:
        # A training row entered 09:31 and held (event_end) to 09:40 overlaps
        # a holdout row entered 09:35 -- same shape as an option position
        # still open when the holdout window starts.
        train = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-08-01 09:31", "2026-08-01 09:00"]),
            "event_end_ts": pd.to_datetime(["2026-08-01 09:40", "2026-08-01 09:05"]),
        })
        heldout = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-08-01 09:35"]),
            "event_end_ts": pd.to_datetime(["2026-08-01 09:45"]),
        })
        kept = apply_event_overlap_purge(train, heldout_frames=[heldout], event_end_col="event_end_ts")
        assert list(kept["timestamp"]) == [pd.Timestamp("2026-08-01 09:00")]

    def test_no_holdout_frames_returns_train_unchanged(self) -> None:
        train = pd.DataFrame({"timestamp": pd.to_datetime(["2026-08-01 09:00"])})
        kept = apply_event_overlap_purge(train, heldout_frames=[], event_end_col="event_end_ts")
        assert len(kept) == 1

    def test_missing_event_end_col_falls_back_to_zero_duration_event(self) -> None:
        # No event_end_ts column at all -- each row's event is treated as
        # instantaneous (event_end == timestamp), so only an exact-overlap
        # training row gets dropped.
        train = pd.DataFrame({"timestamp": pd.to_datetime(["2026-08-01 09:00", "2026-08-01 10:00"])})
        heldout = pd.DataFrame({"timestamp": pd.to_datetime(["2026-08-01 09:00"])})
        kept = apply_event_overlap_purge(train, heldout_frames=[heldout], event_end_col="event_end_ts")
        assert list(kept["timestamp"]) == [pd.Timestamp("2026-08-01 10:00")]

    def test_empty_train_frame_returns_empty(self) -> None:
        train = pd.DataFrame({"timestamp": pd.Series([], dtype="datetime64[ns]")})
        kept = apply_event_overlap_purge(train, heldout_frames=[], event_end_col="event_end_ts")
        assert len(kept) == 0
