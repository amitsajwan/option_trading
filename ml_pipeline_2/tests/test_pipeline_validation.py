"""Regression tests for ml_pipeline_2.pipeline.validation -- fixtures are
the ACTUAL numbers from the real incidents this session that motivated
each check, not synthetic examples."""
from __future__ import annotations

import pytest

from ml_pipeline_2.pipeline.validation import (
    BacktestWindowContaminated,
    ModelDegenerate,
    ParquetInstrumentMismatch,
    RepricingWallFailure,
    check_model_degeneracy,
    check_repricing_wall,
    validate_backtest_window,
    validate_parquet_instrument,
)


class TestValidateBacktestWindow:
    def test_rejects_the_actual_contaminated_window_used_this_session(self) -> None:
        # NIFTY/BankNifty's label-target studies: train_end 2026-06-15,
        # holdout 2026-07-01..07-31, backtest run over 2026-02-01..07-31 --
        # this is the exact overlap that produced a confirmed-false result.
        with pytest.raises(BacktestWindowContaminated):
            validate_backtest_window(
                holdout_end="2026-07-31",
                backtest_from="2026-02-01",
                backtest_to="2026-07-31",
            )

    def test_accepts_the_clean_reverification_window_used_this_session(self) -> None:
        # The window that actually cleared up the contamination finding:
        # 2026-08-03..09-03, strictly after holdout_end 2026-07-31.
        result = validate_backtest_window(
            holdout_end="2026-07-31",
            backtest_from="2026-08-03",
            backtest_to="2026-09-03",
        )
        assert result.ok
        assert result.gap_days == 3

    def test_boundary_exactly_on_holdout_end_is_rejected(self) -> None:
        with pytest.raises(BacktestWindowContaminated):
            validate_backtest_window(
                holdout_end="2026-07-31",
                backtest_from="2026-07-31",
                backtest_to="2026-08-31",
            )

    def test_min_gap_days_enforces_an_embargo(self) -> None:
        # One day after holdout_end passes with no embargo, fails with one.
        validate_backtest_window(
            holdout_end="2026-07-31", backtest_from="2026-08-01", backtest_to="2026-08-10",
        )
        with pytest.raises(BacktestWindowContaminated):
            validate_backtest_window(
                holdout_end="2026-07-31", backtest_from="2026-08-01",
                backtest_to="2026-08-10", min_gap_days=5,
            )

    def test_non_raising_mode_returns_a_result_instead(self) -> None:
        result = validate_backtest_window(
            holdout_end="2026-07-31", backtest_from="2026-02-01",
            backtest_to="2026-07-31", raise_on_fail=False,
        )
        assert not result.ok
        assert "overlap" in result.reason


class TestCheckModelDegeneracy:
    def test_flags_nifty_025pct_as_degenerate_the_actual_confirmed_broken_model(self) -> None:
        # NIFTY's 0.25% target model, 2026-09-09: confirmed structurally
        # broken (precision_fired=0.0, negative separation at every
        # non-degenerate threshold) -- later verified on a clean
        # out-of-sample window to genuinely produce zero trades.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.3, "fire_rate": 0.0001, "precision_fired": 0.0, "separation": -0.0209},
                {"thr": 0.4, "fire_rate": 0.0001, "precision_fired": 0.0, "separation": -0.0209},
                {"thr": 0.5, "fire_rate": 0.0001, "precision_fired": 0.0, "separation": -0.0209},
                {"thr": 0.55, "degenerate": True},
                {"thr": 0.6, "degenerate": True},
                {"thr": 0.65, "degenerate": True},
                {"thr": 0.7, "degenerate": True},
            ],
        }
        with pytest.raises(ModelDegenerate):
            check_model_degeneracy(holdout_eval)

    def test_accepts_banknifty_010pct_the_actual_confirmed_usable_model(self) -> None:
        # BankNifty's 0.10% target model, 2026-09-08: real separation
        # table, precision 0.92/separation 0.776 at thr=0.5 -- confirmed
        # usable and later a genuine (if unconfirmed on live data) multi-
        # metric backtest win.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.3, "fire_rate": 0.0408, "precision_fired": 0.5282, "separation": 0.3982},
                {"thr": 0.5, "fire_rate": 0.0034, "precision_fired": 0.92, "separation": 0.7763},
                {"thr": 0.7, "fire_rate": 0.0014, "precision_fired": 0.9, "separation": 0.7547},
            ],
        }
        result = check_model_degeneracy(holdout_eval)
        assert result.ok
        assert 0.5 in result.usable_thresholds
        assert 0.7 in result.usable_thresholds

    def test_nifty_020pct_modest_but_usable_not_flagged_as_broken(self) -> None:
        # NIFTY's 0.20% target model: healthy full 0-1 prob range but only
        # 14-20% precision everywhere -- weak, but genuinely non-degenerate
        # and above a sensible default bar, so this should NOT raise.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.3, "fire_rate": 0.0122, "precision_fired": 0.1444, "separation": 0.1056},
                {"thr": 0.6, "fire_rate": 0.002, "precision_fired": 0.2, "separation": 0.1602},
            ],
        }
        result = check_model_degeneracy(holdout_eval, min_precision=0.14, min_separation=0.10)
        assert result.ok

    def test_empty_separation_table_is_degenerate(self) -> None:
        with pytest.raises(ModelDegenerate):
            check_model_degeneracy({"separation_table": []})

    def test_non_raising_mode_returns_a_result_instead(self) -> None:
        result = check_model_degeneracy({"separation_table": []}, raise_on_fail=False)
        assert not result.ok
        assert result.usable_thresholds == []

    def test_chosen_threshold_outside_usable_set_is_rejected(self) -> None:
        # FINNIFTY's actual bug: table has usable points, but the DEPLOYED
        # threshold wasn't one of them.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.3, "fire_rate": 0.04, "precision_fired": 0.53, "separation": 0.40},
            ],
        }
        with pytest.raises(ModelDegenerate, match="NOT one of this model's own usable thresholds"):
            check_model_degeneracy(holdout_eval, chosen_threshold=0.57)

    def test_chosen_threshold_inside_usable_set_passes(self) -> None:
        holdout_eval = {
            "separation_table": [
                {"thr": 0.3, "fire_rate": 0.04, "precision_fired": 0.53, "separation": 0.40},
            ],
        }
        result = check_model_degeneracy(holdout_eval, chosen_threshold=0.3)
        assert result.ok
        assert result.chosen_threshold_usable is True

    def test_no_chosen_threshold_only_checks_table_has_some_usable_point(self) -> None:
        holdout_eval = {
            "separation_table": [
                {"thr": 0.5, "fire_rate": 0.0034, "precision_fired": 0.92, "separation": 0.7763},
            ],
        }
        result = check_model_degeneracy(holdout_eval)
        assert result.ok
        assert result.chosen_threshold is None
        assert result.chosen_threshold_usable is None

    def test_excludes_a_row_with_too_few_fired_holdout_rows(self) -> None:
        # BANKNIFTY_015pct's real re-scanned table, 2026-09-10: thr>=0.80
        # shows 100% precision but only ~10-21 fired holdout rows out of
        # 7,383 -- indistinguishable from noise at that sample size.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.70, "fire_rate": 0.0042, "precision_fired": 0.871, "fired_count": 31, "separation": 0.6456},
                {"thr": 0.80, "fire_rate": 0.0028, "precision_fired": 1.0, "fired_count": 10, "separation": 0.7741},
            ],
        }
        result = check_model_degeneracy(holdout_eval, min_fired_count=20)
        assert result.ok
        assert result.usable_thresholds == [0.70]

    def test_a_row_missing_fired_count_entirely_is_not_excluded(self) -> None:
        # Backward compatibility: bundles trained before fired_count
        # existed must not have their usable_thresholds retroactively
        # narrowed just because the field is absent.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.5, "fire_rate": 0.01, "precision_fired": 0.80, "separation": 0.60},
            ],
        }
        result = check_model_degeneracy(holdout_eval, min_fired_count=20)
        assert result.ok
        assert result.usable_thresholds == [0.5]

    def test_default_min_fired_count_is_20(self) -> None:
        holdout_eval = {
            "separation_table": [
                {"thr": 0.5, "fire_rate": 0.01, "precision_fired": 0.80, "fired_count": 19, "separation": 0.60},
            ],
        }
        result = check_model_degeneracy(holdout_eval, raise_on_fail=False)
        assert not result.ok


class TestValidateParquetInstrument:
    def test_rejects_the_actual_sensex_reading_nifty_data_incident(self) -> None:
        # 2026-09-09: a SENSEX backtest's parquet_base actually held NIFTY's
        # data (instrument field "NIFTYFUT") because no instrument-specific
        # path was configured/checked -- produced a real-looking but
        # completely wrong result (4 "trades", -5.9% return).
        with pytest.raises(ParquetInstrumentMismatch, match="NIFTYFUT"):
            validate_parquet_instrument(expected_instrument="SENSEX", actual_instrument="NIFTYFUT")

    def test_accepts_a_matching_instrument(self) -> None:
        result = validate_parquet_instrument(expected_instrument="SENSEX", actual_instrument="SENSEXFUT")
        assert result.ok

    def test_matches_case_insensitively_on_the_expected_side(self) -> None:
        result = validate_parquet_instrument(expected_instrument="sensex", actual_instrument="SENSEXFUT")
        assert result.ok

    def test_non_raising_mode_returns_a_result_instead(self) -> None:
        result = validate_parquet_instrument(
            expected_instrument="BANKNIFTY", actual_instrument="NIFTYFUT", raise_on_fail=False,
        )
        assert not result.ok
        assert "NIFTYFUT" in result.reason


class TestCheckRepricingWall:
    def test_edge_that_survives_delay_passes(self) -> None:
        # Positive at fire, still positive (even if smaller) at +1/+2.
        at_fire = [0.05] * 30
        delay_1 = [0.03] * 30
        delay_2 = [0.02] * 30
        result = check_repricing_wall(payoff_at_fire=at_fire, payoff_delay_1=delay_1, payoff_delay_2=delay_2)
        assert result.ok
        assert result.mean_payoff_at_fire == pytest.approx(0.05)

    def test_edge_that_inverts_by_delay_1_fails(self) -> None:
        # Exactly the pattern that sank the three prior straddle/vertical
        # experiments: looks great at fire-time, negative one bar later.
        at_fire = [0.05] * 30
        delay_1 = [-0.02] * 30
        delay_2 = [-0.03] * 30
        with pytest.raises(RepricingWallFailure, match="repricing-wall"):
            check_repricing_wall(payoff_at_fire=at_fire, payoff_delay_1=delay_1, payoff_delay_2=delay_2)

    def test_edge_that_collapses_to_exactly_zero_fails(self) -> None:
        at_fire = [0.05] * 30
        delay_1 = [0.0] * 30
        delay_2 = [0.01] * 30
        result = check_repricing_wall(
            payoff_at_fire=at_fire, payoff_delay_1=delay_1, payoff_delay_2=delay_2, raise_on_fail=False,
        )
        assert not result.ok

    def test_no_edge_at_fire_time_passes_this_check_with_explanation(self) -> None:
        # Not this check's job to fail an already-losing candidate a second
        # time -- ship gates / degeneracy already cover that.
        at_fire = [-0.01] * 25
        delay_1 = [-0.05] * 25
        delay_2 = [-0.08] * 25
        result = check_repricing_wall(payoff_at_fire=at_fire, payoff_delay_1=delay_1, payoff_delay_2=delay_2)
        assert result.ok
        assert "no positive edge" in result.reason

    def test_none_values_are_dropped_not_imputed(self) -> None:
        at_fire = [0.05, None, 0.06] * 10
        delay_1 = [0.04, 0.04, None] * 10
        delay_2 = [0.03, 0.03, 0.03] * 10
        # Only the (0.05, 0.04, 0.03) triples align across all three -- 10 of
        # them -- below the default min_paired_bars, so lower it here since
        # this test is about the pairing logic, not the sample-size gate.
        result = check_repricing_wall(
            payoff_at_fire=at_fire, payoff_delay_1=delay_1, payoff_delay_2=delay_2, min_paired_bars=5,
        )
        assert result.n_paired_bars == 10

    def test_thin_sample_fails_regardless_of_direction(self) -> None:
        at_fire = [0.05] * 5
        delay_1 = [0.05] * 5
        delay_2 = [0.05] * 5
        with pytest.raises(RepricingWallFailure, match="thin-sample"):
            check_repricing_wall(payoff_at_fire=at_fire, payoff_delay_1=delay_1, payoff_delay_2=delay_2, min_paired_bars=20)

    def test_non_raising_mode_returns_a_result_instead(self) -> None:
        at_fire = [0.05] * 30
        delay_1 = [-0.02] * 30
        delay_2 = [-0.03] * 30
        result = check_repricing_wall(
            payoff_at_fire=at_fire, payoff_delay_1=delay_1, payoff_delay_2=delay_2, raise_on_fail=False,
        )
        assert not result.ok
        assert "repricing-wall" in result.reason
