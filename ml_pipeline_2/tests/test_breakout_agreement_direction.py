"""Tests for ml_pipeline_2.pipeline.breakout_agreement_direction -- hand-
constructed bars where the expected big-move-agreement / fade-the-breakout
call is known exactly, plus sanity checks on the single-sample statistics
helpers (a signal that is truly 50/50 must NOT look significant; a signal
that is truly ~100% correct must)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.breakout_agreement_direction import (
    BREAKOUT_ORW_PCT_MIN,
    BREAKOUT_VOL_SPIKE_MIN,
    CE,
    PE,
    big_move_agreement_vote,
    bootstrap_accuracy_ci,
    breakout_continuation_vote,
    breakout_direction_vote,
    evaluate_signal_accuracy,
    fade_breakout_vote,
    leg_return_for_side,
    opening_range_width_pct,
    permutation_test_vs_baseline,
    variant_agreement_report,
)
from ml_pipeline_2.pipeline.option_payoff_labels import BarPayoff, LegPayoff

REQUIRED_COLS = [
    "fut_return_15m", "price_vs_vwap", "pcr_change_5m",
    "orh", "orl", "orh_broken", "orl_broken", "vol_spike_ratio",
]


def _base_df(n: int) -> pd.DataFrame:
    return pd.DataFrame({
        "fut_return_15m": [0.0] * n,
        "price_vs_vwap": [0.0] * n,
        "pcr_change_5m": [0.0] * n,
        "orh": [24700.0] * n,
        "orl": [24500.0] * n,
        "orh_broken": [False] * n,
        "orl_broken": [False] * n,
        "vol_spike_ratio": [0.0] * n,
    })


class TestBigMoveAgreementVote:
    def test_fires_ce_when_big_move_and_both_signals_agree_bullish(self) -> None:
        df = _base_df(1)
        df.loc[0, "fut_return_15m"] = 0.0025
        df.loc[0, "price_vs_vwap"] = 0.5   # vwap -> CE
        df.loc[0, "pcr_change_5m"] = -0.03  # falling pcr -> CE (vote_pcr_chg)
        votes = big_move_agreement_vote(df, move_threshold=0.0020)
        assert votes.iloc[0] == CE

    def test_fires_pe_when_big_move_and_both_signals_agree_bearish(self) -> None:
        df = _base_df(1)
        df.loc[0, "fut_return_15m"] = -0.0025  # abs() used, sign of move itself irrelevant to gate
        df.loc[0, "price_vs_vwap"] = -0.5   # vwap -> PE
        df.loc[0, "pcr_change_5m"] = 0.03   # rising pcr -> PE
        votes = big_move_agreement_vote(df, move_threshold=0.0020)
        assert votes.iloc[0] == PE

    def test_abstains_when_move_too_small(self) -> None:
        df = _base_df(1)
        df.loc[0, "fut_return_15m"] = 0.0010  # below threshold
        df.loc[0, "price_vs_vwap"] = 0.5
        df.loc[0, "pcr_change_5m"] = -0.03
        votes = big_move_agreement_vote(df, move_threshold=0.0020)
        assert votes.iloc[0] is None

    def test_abstains_when_signals_disagree_even_on_a_big_move(self) -> None:
        df = _base_df(1)
        df.loc[0, "fut_return_15m"] = 0.0030
        df.loc[0, "price_vs_vwap"] = 0.5     # vwap -> CE
        df.loc[0, "pcr_change_5m"] = 0.03    # pcr -> PE (disagree)
        votes = big_move_agreement_vote(df, move_threshold=0.0020)
        assert votes.iloc[0] is None

    def test_abstains_when_either_raw_signal_itself_abstains(self) -> None:
        df = _base_df(1)
        df.loc[0, "fut_return_15m"] = 0.0030
        df.loc[0, "price_vs_vwap"] = 0.0     # vwap itself abstains (==0)
        df.loc[0, "pcr_change_5m"] = -0.03
        votes = big_move_agreement_vote(df, move_threshold=0.0020)
        assert votes.iloc[0] is None

    def test_threshold_is_on_absolute_value_of_move(self) -> None:
        df = _base_df(2)
        df["price_vs_vwap"] = [0.5, -0.5]
        df["pcr_change_5m"] = [-0.03, 0.03]
        df["fut_return_15m"] = [0.0030, -0.0030]
        votes = big_move_agreement_vote(df, move_threshold=0.0020)
        assert list(votes) == [CE, PE]


class TestOpeningRangeWidthPct:
    def test_matches_snapshot_accessor_formula(self) -> None:
        df = pd.DataFrame({"orh": [24700.0], "orl": [24500.0]})
        out = opening_range_width_pct(df)
        assert out.iloc[0] == pytest.approx((24700.0 - 24500.0) / 24500.0)

    def test_zero_or_negative_orl_is_nan_not_a_crash(self) -> None:
        df = pd.DataFrame({"orh": [100.0, 100.0], "orl": [0.0, -5.0]})
        out = opening_range_width_pct(df)
        assert out.isna().all()


class TestBreakoutDirectionVote:
    def test_orh_broken_alone_with_confirmation_votes_ce(self) -> None:
        df = _base_df(1)
        df.loc[0, "orh_broken"] = True
        df.loc[0, "vol_spike_ratio"] = BREAKOUT_VOL_SPIKE_MIN + 0.1
        df.loc[0, "orh"] = 24700.0
        df.loc[0, "orl"] = 24600.0  # width = 100/24600 = 0.00407 >= 0.003
        votes = breakout_direction_vote(df)
        assert votes.iloc[0] == CE

    def test_orl_broken_alone_with_confirmation_votes_pe(self) -> None:
        df = _base_df(1)
        df.loc[0, "orl_broken"] = True
        df.loc[0, "vol_spike_ratio"] = BREAKOUT_VOL_SPIKE_MIN + 0.1
        df.loc[0, "orh"] = 24700.0
        df.loc[0, "orl"] = 24600.0
        votes = breakout_direction_vote(df)
        assert votes.iloc[0] == PE

    def test_both_broken_ties_to_ce_matching_live_regime_quirk(self) -> None:
        df = _base_df(1)
        df.loc[0, "orh_broken"] = True
        df.loc[0, "orl_broken"] = True
        df.loc[0, "vol_spike_ratio"] = BREAKOUT_VOL_SPIKE_MIN + 0.1
        df.loc[0, "orh"] = 24700.0
        df.loc[0, "orl"] = 24600.0
        votes = breakout_direction_vote(df)
        assert votes.iloc[0] == CE

    def test_neither_broken_abstains(self) -> None:
        df = _base_df(1)
        df.loc[0, "vol_spike_ratio"] = 5.0
        df.loc[0, "orh"] = 24900.0
        df.loc[0, "orl"] = 24600.0
        votes = breakout_direction_vote(df)
        assert votes.iloc[0] is None

    def test_weak_volume_vetoes_even_when_broken(self) -> None:
        df = _base_df(1)
        df.loc[0, "orh_broken"] = True
        df.loc[0, "vol_spike_ratio"] = BREAKOUT_VOL_SPIKE_MIN - 0.1  # below cutoff
        df.loc[0, "orh"] = 24700.0
        df.loc[0, "orl"] = 24600.0
        votes = breakout_direction_vote(df)
        assert votes.iloc[0] is None

    def test_narrow_opening_range_vetoes_even_when_broken_and_strong_vol(self) -> None:
        df = _base_df(1)
        df.loc[0, "orh_broken"] = True
        df.loc[0, "vol_spike_ratio"] = BREAKOUT_VOL_SPIKE_MIN + 0.1
        df.loc[0, "orh"] = 24610.0
        df.loc[0, "orl"] = 24600.0  # width = 10/24600 = 0.0004 < 0.003
        votes = breakout_direction_vote(df)
        assert votes.iloc[0] is None

    def test_unconfirmed_ablation_ignores_vol_and_orw(self) -> None:
        df = _base_df(1)
        df.loc[0, "orh_broken"] = True
        df.loc[0, "vol_spike_ratio"] = 0.0  # would veto if confirmation required
        df.loc[0, "orh"] = 24610.0
        df.loc[0, "orl"] = 24600.0  # narrow -- would also veto if confirmation required
        votes = breakout_direction_vote(df, require_confirmation=False)
        assert votes.iloc[0] == CE


class TestFadeAndContinuationVote:
    def test_fade_is_the_exact_opposite_of_continuation_on_every_fired_bar(self) -> None:
        df = _base_df(4)
        df["orh_broken"] = [True, False, True, False]
        df["orl_broken"] = [False, True, False, False]
        df["vol_spike_ratio"] = [BREAKOUT_VOL_SPIKE_MIN + 1] * 4
        df["orh"] = [24700.0] * 4
        df["orl"] = [24600.0] * 4
        cont = breakout_continuation_vote(df)
        fade = fade_breakout_vote(df)
        assert list(cont) == [CE, PE, CE, None]
        assert list(fade) == [PE, CE, PE, None]

    def test_fade_never_fires_where_continuation_abstains(self) -> None:
        df = _base_df(1)  # neither orh_broken nor orl_broken
        assert fade_breakout_vote(df).iloc[0] is None


class TestLegReturnForSide:
    def _bp(self, ce_ret: float | None, pe_ret: float | None) -> BarPayoff:
        ce = LegPayoff("CE", 100.0, 100.0 * (1 + ce_ret), 50000.0, 0.01, ce_ret) if ce_ret is not None else None
        pe = LegPayoff("PE", 100.0, 100.0 * (1 + pe_ret), 50000.0, 0.01, pe_ret) if pe_ret is not None else None
        return BarPayoff(strike=24500, ce=ce, pe=pe)

    def test_picks_the_called_leg_even_when_it_is_the_losing_one(self) -> None:
        bp = self._bp(ce_ret=-0.10, pe_ret=0.20)
        assert leg_return_for_side(bp, CE) == pytest.approx(-0.10)
        assert leg_return_for_side(bp, PE) == pytest.approx(0.20)

    def test_none_bar_payoff_or_side_returns_none(self) -> None:
        bp = self._bp(ce_ret=0.05, pe_ret=-0.02)
        assert leg_return_for_side(None, CE) is None
        assert leg_return_for_side(bp, None) is None

    def test_missing_leg_quote_returns_none(self) -> None:
        bp = self._bp(ce_ret=None, pe_ret=0.05)
        assert leg_return_for_side(bp, CE) is None
        assert leg_return_for_side(bp, PE) == pytest.approx(0.05)


class TestSingleSampleStatistics:
    def test_a_coin_flip_signal_is_not_significant(self) -> None:
        rng = np.random.default_rng(7)
        correct = rng.random(4000) < 0.50
        p = permutation_test_vs_baseline(correct, null_accuracy=0.5, n_perm=2000, seed=1)
        assert p > 0.05
        lo, hi = bootstrap_accuracy_ci(correct, n_boot=1000, seed=1)
        assert lo < 0.5 < hi

    def test_a_strongly_accurate_signal_is_significant_with_a_ci_excluding_half(self) -> None:
        rng = np.random.default_rng(9)
        correct = rng.random(500) < 0.75
        p = permutation_test_vs_baseline(correct, null_accuracy=0.5, n_perm=2000, seed=2)
        assert p < 0.01
        lo, hi = bootstrap_accuracy_ci(correct, n_boot=1000, seed=2)
        assert lo > 0.5

    def test_empty_input_returns_none_not_a_crash(self) -> None:
        assert permutation_test_vs_baseline([], n_perm=100) is None
        lo, hi = bootstrap_accuracy_ci([], n_boot=100)
        assert lo is None and hi is None

    def test_evaluate_signal_accuracy_reports_coverage_and_stats_together(self) -> None:
        df = _base_df(6)
        df["fut_return_15m"] = [0.0030] * 6
        df["price_vs_vwap"] = [0.5, 0.5, 0.5, -0.5, 0.0, 0.5]
        df["pcr_change_5m"] = [-0.03, -0.03, -0.03, 0.03, -0.03, -0.03]
        truth = pd.Series(["CE", "CE", "PE", "PE", "CE", "CE"])
        votes = big_move_agreement_vote(df, move_threshold=0.0020)
        result = evaluate_signal_accuracy(votes, truth, n_boot=200, n_perm=200)
        # fires on rows 0,1,2,3,5 (row 4 abstains: vwap==0); correct on 0,1,3,5
        # (row 2 votes CE but truth is PE) -> 4/5
        assert result["n_voted"] == 5
        assert result["accuracy"] == pytest.approx(0.8)
        assert result["bootstrap_ci_low"] is not None
        assert result["permutation_p_value"] is not None


class TestVariantAgreementReport:
    def test_reports_zero_when_variants_never_both_fire(self) -> None:
        a = pd.Series([None, None, "CE"])
        b = pd.Series([None, "PE", None])
        t = pd.Series(["CE", "PE", "CE"])
        out = variant_agreement_report(a, b, t)
        assert out["n_both_fired"] == 0

    def test_computes_agreement_rate_and_per_variant_accuracy_on_shared_bars(self) -> None:
        a = pd.Series(["CE", "PE", "CE", "PE"])
        b = pd.Series(["CE", "CE", "PE", "PE"])
        t = pd.Series(["CE", "CE", "CE", "PE"])
        out = variant_agreement_report(a, b, t)
        assert out["n_both_fired"] == 4
        # agree on rows 0 and 3 -> 2/4
        assert out["agreement_rate"] == pytest.approx(0.5)
        # a correct on rows 0,2,3 -> 3/4; b correct on rows 0,1,3 -> 3/4
        assert out["accuracy_variant_a_on_shared_bars"] == pytest.approx(0.75)
        assert out["accuracy_variant_b_on_shared_bars"] == pytest.approx(0.75)
        # when they agree (rows 0,3), both call CE/PE resp., truth CE/PE -> both correct -> 1.0
        assert out["accuracy_when_variants_agree"] == pytest.approx(1.0)
