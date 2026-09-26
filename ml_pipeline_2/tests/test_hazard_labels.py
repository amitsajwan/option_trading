"""Tests for the hazard/duration-to-move entry-timing label
(`ml_pipeline_2.pipeline.hazard_labels`) -- the survival-analysis
reformulation of `train_entry_dhan_v3.add_labels`'s fixed-horizon binary
label. Fixture convention mirrors
`test_train_entry_dhan_v3_labels.py::_synthetic_session_df` (one synthetic
trading day, one bar per minute starting 09:45 IST).
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from ml_pipeline_2.pipeline.hazard_labels import (
    add_hazard_labels,
    concordance_index,
    evaluate_hazard,
)


def _session_df(closes: list[float], *, day: str = "2026-01-05", start: str = "09:45") -> pd.DataFrame:
    """One synthetic trading day, one bar per minute starting at `start`
    IST, with `closes` as the per-minute close price."""
    n = len(closes)
    timestamps = pd.date_range(f"{day} {start}", periods=n, freq="1min")
    return pd.DataFrame({
        "trade_date": pd.Timestamp(day),
        "timestamp": timestamps,
        "close": closes,
    })


# ── Label construction ──────────────────────────────────────────────────────

class TestAddHazardLabelsBasic:
    def test_immediate_next_bar_event(self) -> None:
        # Flat run, then a >100pt jump at the very next bar after index 19.
        closes = [50000.0] * 20 + [50150.0] * 20
        df = add_hazard_labels(_session_df(closes))
        row = df.iloc[19]
        assert row["event_observed"] == 1.0
        assert row["time_to_event"] == 1.0

    def test_event_several_bars_out(self) -> None:
        # Flat for 10 bars, then a +20pt/bar ramp for 10 more bars. From the
        # last flat bar (index 9, close=50000), the default 100pt threshold
        # is first crossed at the ramp's 5th bar (index 14, close=50100):
        # k=5 -> 50000 + 5*20 = 50100 >= 100pt move.
        closes = [50000.0] * 10 + [50000.0 + 20.0 * i for i in range(1, 11)]
        df = add_hazard_labels(_session_df(closes))
        row = df.iloc[9]
        assert row["event_observed"] == 1.0
        assert row["time_to_event"] == 5.0

    def test_no_event_before_session_end_is_censored(self) -> None:
        # Flat for the whole day -- no bar ever sees a qualifying move.
        closes = [50000.0] * 40
        df = add_hazard_labels(_session_df(closes))
        mid = 15
        row = df.iloc[mid]
        assert row["event_observed"] == 0.0
        # 40 bars total (0..39); from index 15, 39 - 15 = 24 bars remain.
        assert row["time_to_event"] == 24.0

    def test_bar_very_close_to_session_end_is_censored_not_a_false_short_time(self) -> None:
        # Last two bars of the day both stay well under threshold -- the
        # second-to-last bar has exactly 1 bar left to look forward into,
        # and must come out censored (event_observed=False) with
        # time_to_event=1 (the true data-boundary distance), never treated
        # as an "event" just because the loop ran out of future bars to scan.
        closes = [50000.0] * 38 + [50005.0, 50008.0]
        df = add_hazard_labels(_session_df(closes))
        second_last = df.iloc[38]
        last = df.iloc[39]
        assert second_last["event_observed"] == 0.0
        assert second_last["time_to_event"] == 1.0
        # The very last bar has zero future bars at all -- also censored,
        # not an event, with time_to_event=0 (not NaN, not a crash).
        assert last["event_observed"] == 0.0
        assert last["time_to_event"] == 0.0


class TestAddHazardLabelsPercentMode:
    def test_label_pct_scales_threshold_to_the_rows_own_close(self) -> None:
        # 0.10% of 50000 = 50pt -- a 60pt move at the next bar should fire.
        closes = [50000.0] * 20 + [50060.0] * 20
        df = add_hazard_labels(_session_df(closes), label_pct=0.10)
        row = df.iloc[19]
        assert row["event_observed"] == 1.0
        assert row["time_to_event"] == 1.0

    def test_rejects_both_label_pct_and_label_pt(self) -> None:
        df = _session_df([50000.0] * 20)
        with pytest.raises(ValueError, match="at most one of label_pct / label_pt"):
            add_hazard_labels(df, label_pct=0.10, label_pt=50.0)


class TestAddHazardLabelsSessionWindow:
    def test_bars_before_session_start_get_nan(self) -> None:
        # 09:30-10:10 (41 bars): the first 15 (09:30-09:44) are before the
        # 9:45 session start and must get NaN, not a real (mis)computed value.
        closes = [50000.0] * 41
        df = add_hazard_labels(_session_df(closes, start="09:30"))
        before_window = df[df["timestamp"].dt.strftime("%H:%M") < "09:45"]
        in_window = df[df["timestamp"].dt.strftime("%H:%M") >= "09:45"]
        assert len(before_window) == 15
        assert before_window["time_to_event"].isna().all()
        assert before_window["event_observed"].isna().all()
        assert in_window["event_observed"].notna().all()

    def test_bars_after_session_end_get_nan(self) -> None:
        # 15:00-15:10 (11 bars): bars after 15:05 must get NaN.
        closes = [50000.0] * 11
        df = add_hazard_labels(_session_df(closes, start="15:00"))
        after_window = df[df["timestamp"].dt.strftime("%H:%M") > "15:05"]
        assert len(after_window) == 5  # 15:06, 15:07, 15:08, 15:09, 15:10
        assert after_window["time_to_event"].isna().all()
        assert after_window["event_observed"].isna().all()


class TestAddHazardLabelsDayBoundary:
    def test_day_boundary_is_never_crossed(self) -> None:
        # Day 1: 20 flat bars (no move). Day 2: opens with an enormous jump
        # relative to day 1's close. If the walk-forward scan leaked across
        # the day boundary, day 1's last bar would see day 2's jump and
        # register a (bogus) event. It must not: day 1's own group has only
        # 20 rows, so its last bar (index 19) has zero bars left within its
        # OWN day and must be censored with time_to_event=0, regardless of
        # what day 2 does.
        day1 = _session_df([50000.0] * 20, day="2026-01-05")
        day2 = _session_df([60000.0] * 20, day="2026-01-06")  # +10000pt "jump"
        combined = pd.concat([day1, day2], ignore_index=True)
        df = add_hazard_labels(combined)
        last_of_day1 = df[df["trade_date"] == pd.Timestamp("2026-01-05")].iloc[-1]
        assert last_of_day1["event_observed"] == 0.0
        assert last_of_day1["time_to_event"] == 0.0

    def test_day_boundary_does_not_shorten_a_real_within_day_censored_time(self) -> None:
        # Same as above but checked one bar earlier (index 18, 1 bar left
        # in day 1): must be censored with time_to_event=1, not accidentally
        # pick up day 2's opening bar as its "next" bar.
        day1 = _session_df([50000.0] * 20, day="2026-01-05")
        day2 = _session_df([60000.0] * 20, day="2026-01-06")
        combined = pd.concat([day1, day2], ignore_index=True)
        df = add_hazard_labels(combined)
        second_last_of_day1 = df[df["trade_date"] == pd.Timestamp("2026-01-05")].iloc[-2]
        assert second_last_of_day1["event_observed"] == 0.0
        assert second_last_of_day1["time_to_event"] == 1.0


# ── concordance_index ────────────────────────────────────────────────────────

class TestConcordanceIndex:
    def test_perfect_concordance(self) -> None:
        # 3 fully-observed rows, predicted order matches actual order
        # exactly -> all 3 pairs comparable and concordant.
        predicted = [1.0, 2.0, 3.0]
        actual = [1.0, 2.0, 3.0]
        observed = [True, True, True]
        result = concordance_index(predicted, actual, observed)
        assert result.n_comparable == 3
        assert result.n_concordant == 3
        assert result.n_discordant == 0
        assert result.c_index == pytest.approx(1.0)

    def test_worst_concordance(self) -> None:
        # Predicted order is exactly reversed from actual -> all discordant.
        predicted = [3.0, 2.0, 1.0]
        actual = [1.0, 2.0, 3.0]
        observed = [True, True, True]
        result = concordance_index(predicted, actual, observed)
        assert result.n_comparable == 3
        assert result.n_discordant == 3
        assert result.c_index == pytest.approx(0.0)

    def test_tied_prediction_counts_as_half_credit(self) -> None:
        # Two observed rows with different actual times but an identical
        # predicted value -> the pair is comparable but the prediction
        # can't distinguish them: half credit, by convention.
        predicted = [5.0, 5.0]
        actual = [1.0, 2.0]
        observed = [True, True]
        result = concordance_index(predicted, actual, observed)
        assert result.n_comparable == 1
        assert result.n_tied_prediction == 1
        assert result.c_index == pytest.approx(0.5)

    def test_censored_pair_comparable_when_observed_time_precedes_censor_time(self) -> None:
        # Row A: observed at t=2. Row B: censored at t=5 (2 < 5) -> A is
        # DEFINITELY before B (B's true event is >= 5). Predicted times
        # agree with that order -> concordant.
        predicted = [1.0, 9.0]
        actual = [2.0, 5.0]
        observed = [True, False]
        result = concordance_index(predicted, actual, observed)
        assert result.n_comparable == 1
        assert result.n_concordant == 1
        assert result.c_index == pytest.approx(1.0)

    def test_censored_pair_not_comparable_when_observed_time_is_after_censor_time(self) -> None:
        # Row A: observed at t=6. Row B: censored at t=5. 6 is NOT < 5, so
        # we cannot tell whether A or B's true event came first (B's true
        # event could be anywhere >= 5) -> not comparable, at all.
        predicted = [1.0, 9.0]
        actual = [6.0, 5.0]
        observed = [True, False]
        result = concordance_index(predicted, actual, observed)
        assert result.n_comparable == 0
        assert math.isnan(result.c_index)

    def test_both_censored_pair_is_never_comparable(self) -> None:
        predicted = [1.0, 9.0]
        actual = [3.0, 3.0]
        observed = [False, False]
        result = concordance_index(predicted, actual, observed)
        assert result.n_comparable == 0
        assert math.isnan(result.c_index)

    def test_rejects_mismatched_lengths(self) -> None:
        with pytest.raises(ValueError):
            concordance_index([1.0, 2.0], [1.0], [True, True])


# ── evaluate_hazard ──────────────────────────────────────────────────────────

class TestEvaluateHazard:
    def test_monotonic_true_when_soonest_bucket_has_shorter_realized_time(self) -> None:
        # Two clean groups: low predicted-time with short realized times,
        # high predicted-time with long realized times.
        predicted = [1.0, 1.2, 1.5, 10.0, 11.0, 12.0]
        actual = [1.0, 2.0, 3.0, 10.0, 12.0, 15.0]
        observed = [True] * 6
        result = evaluate_hazard(predicted, actual, observed, n_buckets=2)
        assert result.monotonic is True
        assert result.buckets[0].realized_median_time < result.buckets[1].realized_median_time

    def test_monotonic_false_when_calibration_is_inverted(self) -> None:
        # Predicted "soon" bucket actually realizes LONGER times than the
        # predicted "later" bucket -- a badly-calibrated model.
        predicted = [1.0, 1.2, 1.5, 10.0, 11.0, 12.0]
        actual = [10.0, 12.0, 15.0, 1.0, 2.0, 3.0]
        observed = [True] * 6
        result = evaluate_hazard(predicted, actual, observed, n_buckets=2)
        assert result.monotonic is False

    def test_bucket_with_no_observed_rows_gets_none_median_and_is_skipped(self) -> None:
        # 3 buckets: bucket 0 (soonest predicted) is all-censored -> no
        # realized median available. Buckets 1 and 2 are observed and
        # correctly ordered. Overall monotonicity should still evaluate
        # (skipping the undefined bucket), not break.
        predicted = [1.0, 1.1, 5.0, 5.1, 10.0, 10.1]
        actual = [3.0, 3.0, 5.0, 6.0, 10.0, 11.0]
        observed = [False, False, True, True, True, True]
        result = evaluate_hazard(predicted, actual, observed, n_buckets=3)
        assert result.buckets[0].realized_median_time is None
        assert result.buckets[1].realized_median_time is not None
        assert result.buckets[2].realized_median_time is not None
        assert result.monotonic is True

    def test_rejects_invalid_n_buckets(self) -> None:
        with pytest.raises(ValueError):
            evaluate_hazard([1.0], [1.0], [True], n_buckets=0)
