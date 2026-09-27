"""Tests for ml_pipeline_2.pipeline.conditional_direction -- pre-existing
module (2026-09-26 prior research session) with no test coverage before
this file. Covers: per-signal vote replication against the transcribed
resolver weights/thresholds, the reduced composite vote, decile assignment
and per-decile accuracy scoring, expanding-window walk-forward construction,
the paired bootstrap/permutation significance test, and the delayed-action
repricing-wall check."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.conditional_direction import (
    CE,
    PE,
    COMPOSITE_MIN_MARGIN,
    IV_SKEW_CE_RATIO_LOW,
    IV_SKEW_PE_RATIO_HIGH,
    PCR_CHG_THRESHOLD,
    REQUIRED_RAW_COLUMNS,
    VIX_CHG_THRESHOLD,
    DirectionRepricingWallFailure,
    accuracy_and_coverage,
    assign_predicted_deciles,
    build_expanding_windows,
    check_direction_repricing_wall,
    composite_vote,
    paired_accuracy_comparison,
    signal_accuracy_by_decile,
    vote_iv_skew,
    vote_momentum_5m,
    vote_momentum_15m,
    vote_or_trap,
    vote_pcr_chg,
    vote_vix_chg,
    vote_vwap_side,
)


def _base_df(n: int) -> pd.DataFrame:
    """A frame with every REQUIRED_RAW_COLUMNS present, all neutral/zero --
    tests override only the columns relevant to the signal under test."""
    return pd.DataFrame({col: [0.0] * n for col in REQUIRED_RAW_COLUMNS})


class TestVoteMomentum:
    def test_positive_return_votes_ce(self) -> None:
        df = _base_df(3)
        df["fut_return_5m"] = [1.0, -1.0, 0.0]
        votes = vote_momentum_5m(df)
        assert list(votes) == [CE, PE, None]

    def test_momentum_15m_same_rule_as_5m(self) -> None:
        df = _base_df(2)
        df["fut_return_15m"] = [2.5, -0.1]
        assert list(vote_momentum_15m(df)) == [CE, PE]

    def test_nan_is_not_a_vote(self) -> None:
        df = _base_df(1)
        df["fut_return_5m"] = [np.nan]
        assert vote_momentum_5m(df).iloc[0] is None


class TestVoteVwapSide:
    def test_above_vwap_is_ce_below_is_pe(self) -> None:
        df = _base_df(2)
        df["price_vs_vwap"] = [0.3, -0.2]
        assert list(vote_vwap_side(df)) == [CE, PE]


class TestVoteVixChg:
    def test_fires_only_above_threshold(self) -> None:
        df = _base_df(4)
        df["vix_intraday_chg"] = [VIX_CHG_THRESHOLD + 0.1, -VIX_CHG_THRESHOLD - 0.1, 1.0, -1.0]
        votes = vote_vix_chg(df)
        # rising VIX (fear) -> PE, falling VIX -> CE; below-threshold abstains
        assert list(votes) == [PE, CE, None, None]


class TestVoteIvSkew:
    def test_pe_richer_votes_pe_ce_richer_votes_ce(self) -> None:
        df = _base_df(3)
        df["atm_ce_iv"] = [10.0, 10.0, 10.0]
        df["atm_pe_iv"] = [10.0 * IV_SKEW_PE_RATIO_HIGH + 1.0, 10.0 * IV_SKEW_CE_RATIO_LOW - 1.0, 10.5]
        votes = vote_iv_skew(df)
        assert votes.iloc[0] == PE
        assert votes.iloc[1] == CE
        assert votes.iloc[2] is None  # inside the neutral band

    def test_zero_or_negative_iv_never_fires(self) -> None:
        df = _base_df(1)
        df["atm_ce_iv"] = [0.0]
        df["atm_pe_iv"] = [20.0]
        assert vote_iv_skew(df).iloc[0] is None


class TestVoteOrTrap:
    def test_failed_breakdown_recovery_votes_ce(self) -> None:
        df = _base_df(1)
        df["orh"] = [24700.0]
        df["orl"] = [24500.0]
        df["orl_broken"] = [True]
        df["orh_broken"] = [False]
        df["fut_close"] = [24550.0]  # back above orl
        assert vote_or_trap(df).iloc[0] == CE

    def test_failed_breakout_rejection_votes_pe(self) -> None:
        df = _base_df(1)
        df["orh"] = [24700.0]
        df["orl"] = [24500.0]
        df["orh_broken"] = [True]
        df["orl_broken"] = [False]
        df["fut_close"] = [24650.0]  # back below orh
        assert vote_or_trap(df).iloc[0] == PE

    def test_both_fire_at_once_is_treated_as_ambiguous_non_vote(self) -> None:
        df = _base_df(1)
        df["orh"] = [24700.0]
        df["orl"] = [24500.0]
        df["orh_broken"] = [True]
        df["orl_broken"] = [True]
        df["fut_close"] = [24600.0]  # > orl and < orh -> both fire
        assert vote_or_trap(df).iloc[0] is None

    def test_or_not_ready_never_fires(self) -> None:
        df = _base_df(1)
        df["orh"] = [np.nan]
        df["orl"] = [24500.0]
        df["orl_broken"] = [True]
        df["fut_close"] = [24600.0]
        assert vote_or_trap(df).iloc[0] is None


class TestVotePcrChg:
    def test_falling_pcr_votes_ce_rising_votes_pe(self) -> None:
        df = _base_df(3)
        df["pcr_change_5m"] = [-PCR_CHG_THRESHOLD - 0.01, PCR_CHG_THRESHOLD + 0.01, 0.001]
        votes = vote_pcr_chg(df)
        assert list(votes) == [CE, PE, None]


class TestCompositeVote:
    def test_agreeing_signals_produce_a_vote_above_min_margin(self) -> None:
        df = _base_df(1)
        df["fut_return_5m"] = [2.0]
        df["fut_return_15m"] = [2.0]
        df["price_vs_vwap"] = [0.5]
        assert composite_vote(df).iloc[0] == CE

    def test_no_signals_firing_is_a_veto(self) -> None:
        df = _base_df(1)
        assert composite_vote(df).iloc[0] is None

    def test_below_min_margin_is_vetoed(self) -> None:
        # momentum_5m (weight 1.0, CE) vs vix_chg (weight 0.75, PE) -> margin
        # 0.25, below COMPOSITE_MIN_MARGIN=0.35 -> vetoed even though not tied.
        df = _base_df(1)
        df["fut_return_5m"] = [1.0]
        df["vix_intraday_chg"] = [VIX_CHG_THRESHOLD + 1.0]
        assert composite_vote(df).iloc[0] is None
        assert COMPOSITE_MIN_MARGIN > 0  # sanity: the constant this test relies on exists


class TestAccuracyAndCoverage:
    def test_perfect_accuracy_full_coverage(self) -> None:
        votes = pd.Series([CE, PE, CE])
        truth = pd.Series([CE, PE, CE])
        stats = accuracy_and_coverage(votes, truth)
        assert stats["accuracy"] == 1.0
        assert stats["coverage"] == 1.0
        assert stats["n_voted"] == 3

    def test_abstentions_reduce_coverage_not_accuracy(self) -> None:
        votes = pd.Series([CE, None, PE])
        truth = pd.Series([CE, CE, PE])
        stats = accuracy_and_coverage(votes, truth)
        assert stats["n_voted"] == 2
        assert stats["coverage"] == pytest.approx(2 / 3, abs=1e-3)
        assert stats["accuracy"] == 1.0

    def test_zero_votes_returns_none_accuracy_not_crash(self) -> None:
        votes = pd.Series([None, None])
        truth = pd.Series([CE, PE])
        stats = accuracy_and_coverage(votes, truth)
        assert stats["accuracy"] is None
        assert stats["n_voted"] == 0


class TestAssignPredictedDeciles:
    def test_highest_predicted_values_get_top_decile_label(self) -> None:
        predicted = np.arange(100, dtype=float)
        deciles = assign_predicted_deciles(predicted, n_deciles=10)
        assert deciles[-1] == 10  # highest predicted value
        assert deciles[0] == 1    # lowest predicted value

    def test_equal_count_buckets(self) -> None:
        predicted = np.arange(100, dtype=float)
        deciles = assign_predicted_deciles(predicted, n_deciles=10)
        counts = pd.Series(deciles).value_counts()
        assert set(counts.values) == {10}


class TestSignalAccuracyByDecile:
    def test_top_decile_more_accurate_when_signal_and_truth_align_there(self) -> None:
        n = 200
        rng = np.random.default_rng(0)
        df = _base_df(n)
        # momentum_5m always votes CE (positive); truth is CE 90% of the time
        # in the top half (by construction) and 50/50 in the bottom half --
        # a deliberately planted decile-dependent edge to check the mechanism
        # actually detects one when it's really there.
        df["fut_return_5m"] = 1.0
        predicted = np.arange(n, dtype=float)
        truth = []
        for i in range(n):
            if i >= n // 2:
                truth.append(CE if rng.random() < 0.9 else PE)
            else:
                truth.append(CE if rng.random() < 0.5 else PE)
        df["payoff_winning_side"] = truth
        deciles = assign_predicted_deciles(predicted, n_deciles=10)
        table = signal_accuracy_by_decile(df, deciles, include_composite=False)
        mom = table[table["signal"] == "momentum_5m"].set_index("decile")
        assert mom.loc[10, "accuracy"] > mom.loc[1, "accuracy"]


class TestBuildExpandingWindows:
    def test_windows_are_sequential_and_non_overlapping(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(1, 31)]
        windows = build_expanding_windows(dates, n_windows=3, min_train_days=10)
        assert len(windows) == 3
        # each window's test chunk starts right after the previous one ends
        assert windows[0].test_start > windows[0].train_end
        for i in range(1, len(windows)):
            assert windows[i].test_start > windows[i - 1].test_end
            # expanding origin: later windows' train sets start no later
            assert windows[i].train_end >= windows[i - 1].train_end

    def test_raises_when_not_enough_days_for_seed_train(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(1, 5)]
        with pytest.raises(ValueError, match="min_train_days"):
            build_expanding_windows(dates, n_windows=2, min_train_days=10)

    def test_raises_when_not_enough_remaining_days_for_n_windows(self) -> None:
        dates = [f"2026-01-{d:02d}" for d in range(1, 12)]
        with pytest.raises(ValueError, match="not enough to form"):
            build_expanding_windows(dates, n_windows=5, min_train_days=10)


class TestPairedAccuracyComparison:
    def test_identical_arrays_give_zero_diff_and_high_p_value(self) -> None:
        correct = [True, False, True, True, False] * 20
        result = paired_accuracy_comparison(correct, correct)
        assert result.observed_diff == 0.0
        assert result.permutation_p_value > 0.5

    def test_a_strictly_better_than_b_gives_positive_diff_and_low_p_value(self) -> None:
        rng = np.random.default_rng(0)
        n = 300
        # a is right 90% of the time, b is right 50% of the time, independent
        a = (rng.random(n) < 0.9).tolist()
        b = (rng.random(n) < 0.5).tolist()
        result = paired_accuracy_comparison(a, b, n_perm=2000)
        assert result.observed_diff > 0.2
        assert result.permutation_p_value < 0.05

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError, match="paired"):
            paired_accuracy_comparison([True, False], [True])

    def test_empty_input_returns_none_fields_not_crash(self) -> None:
        result = paired_accuracy_comparison([], [])
        assert result.n == 0
        assert result.accuracy_a is None


class TestCheckDirectionRepricingWall:
    def _n_bars(self, n: int, *, votes, truth) -> tuple[list, list]:
        return [votes] * n, [truth] * n

    def test_thin_sample_reports_not_ok_without_raising_when_disabled(self) -> None:
        votes, truth = self._n_bars(5, votes=CE, truth=CE)
        report = check_direction_repricing_wall(
            votes_at_fire=votes, truth_at_fire=truth,
            votes_delay_1=votes, truth_delay_1=truth,
            votes_delay_2=votes, truth_delay_2=truth,
            min_paired_bars=20, raise_on_fail=False,
        )
        assert report.ok is False
        assert "only" in report.reason  # descriptive thin-sample explanation, not just a bare False

    def test_thin_sample_raises_when_enabled(self) -> None:
        votes, truth = self._n_bars(5, votes=CE, truth=CE)
        with pytest.raises(DirectionRepricingWallFailure):
            check_direction_repricing_wall(
                votes_at_fire=votes, truth_at_fire=truth,
                votes_delay_1=votes, truth_delay_1=truth,
                votes_delay_2=votes, truth_delay_2=truth,
                min_paired_bars=20, raise_on_fail=True,
            )

    def test_no_edge_at_fire_time_reports_ok_true(self) -> None:
        # Below edge_threshold at fire -- nothing to collapse, reports ok.
        n = 30
        votes_fire = [CE] * (n // 2) + [PE] * (n // 2)
        truth_fire = [CE] * (n // 4) + [PE] * (n // 4) + [CE] * (n // 4) + [PE] * (n // 4)
        report = check_direction_repricing_wall(
            votes_at_fire=votes_fire, truth_at_fire=truth_fire,
            votes_delay_1=votes_fire, truth_delay_1=truth_fire,
            votes_delay_2=votes_fire, truth_delay_2=truth_fire,
            min_paired_bars=20, edge_threshold=0.55, raise_on_fail=False,
        )
        assert report.ok is True
        assert "no above-chance edge" in report.reason

    def test_apparent_edge_that_collapses_at_delay_fails(self) -> None:
        n = 40
        votes = [CE] * n
        truth_fire = [CE] * 36 + [PE] * 4       # 90% accuracy at fire
        truth_delay1 = [CE] * 18 + [PE] * 22    # collapses to <=50%
        truth_delay2 = [CE] * 16 + [PE] * 24
        report = check_direction_repricing_wall(
            votes_at_fire=votes, truth_at_fire=truth_fire,
            votes_delay_1=votes, truth_delay_1=truth_delay1,
            votes_delay_2=votes, truth_delay_2=truth_delay2,
            min_paired_bars=20, edge_threshold=0.55, raise_on_fail=False,
        )
        assert report.ok is False
        assert "collapses" in report.reason

    def test_apparent_edge_that_survives_delay_passes(self) -> None:
        n = 40
        votes = [CE] * n
        truth_fire = [CE] * 36 + [PE] * 4
        truth_delay1 = [CE] * 34 + [PE] * 6
        truth_delay2 = [CE] * 33 + [PE] * 7
        report = check_direction_repricing_wall(
            votes_at_fire=votes, truth_at_fire=truth_fire,
            votes_delay_1=votes, truth_delay_1=truth_delay1,
            votes_delay_2=votes, truth_delay_2=truth_delay2,
            min_paired_bars=20, edge_threshold=0.55, raise_on_fail=False,
        )
        assert report.ok is True
        assert report.reason == ""

    def test_none_entries_are_dropped_not_imputed(self) -> None:
        votes = [CE, CE, None, CE] * 10
        truth = [CE, PE, CE, None] * 10
        report = check_direction_repricing_wall(
            votes_at_fire=votes, truth_at_fire=truth,
            votes_delay_1=votes, truth_delay_1=truth,
            votes_delay_2=votes, truth_delay_2=truth,
            min_paired_bars=5, edge_threshold=0.9, raise_on_fail=False,
        )
        # only rows with all 6 fields present count -- half of each group of 4
        assert report.n_paired_bars == 20
