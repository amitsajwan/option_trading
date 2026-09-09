"""Regression tests for ml_pipeline_2.pipeline.validation -- fixtures are
the ACTUAL numbers from the two real incidents this session that motivated
each check, not synthetic examples."""
from __future__ import annotations

import pytest

from ml_pipeline_2.pipeline.validation import (
    BacktestWindowContaminated,
    ModelDegenerate,
    check_model_degeneracy,
    validate_backtest_window,
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
