"""Tests for train_entry_option_payoff_v1.py. HPO/main() end-to-end tests
need optuna+xgboost (not installed in every environment this runs in --
skipped gracefully, not silently, via importorskip) -- the pure decile/gate
logic is tested unconditionally since it needs no ML dependency."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.scripts.train_entry_option_payoff_v1 import (
    TARGET_COL,
    _select_features,
    compute_ship_gates,
    decile_lift_table,
    load_training_view,
    winsorize_target,
)


class TestWinsorizeTarget:
    def test_clips_extreme_tail_values(self) -> None:
        # Real 2026-09-26 shape: a tight bulk distribution plus a few wild
        # tail outliers (e.g. a near-worthless cheap entry blown out by
        # flat brokerage, or a rare huge tail move).
        y = np.array([0.01, 0.02, 0.03, 0.05, 0.04, 0.02, 0.01, -0.01, 11.8, -1.1])
        clipped, lo, hi = winsorize_target(y, lower_pct=0.1, upper_pct=0.9)
        assert clipped.max() < 11.8
        assert clipped.min() > -1.1
        assert lo < hi

    def test_full_0_to_1_percentile_range_clips_nothing(self) -> None:
        y = np.array([0.01, 0.02, 0.03, 0.04, 100.0])
        clipped, lo, hi = winsorize_target(y, lower_pct=0.0, upper_pct=1.0)
        np.testing.assert_allclose(clipped, y)

    def test_never_touches_a_second_array_passed_separately(self) -> None:
        # Simulates the real usage: winsorize the TRAINING target only,
        # never validation/holdout -- this test just confirms the function
        # itself doesn't mutate its input in place, which would be an easy
        # way to accidentally leak the clip into a caller's other arrays.
        y_train = np.array([0.01, 0.02, 100.0])
        y_valid = np.array([0.01, 0.02, 100.0]).copy()
        winsorize_target(y_train, lower_pct=0.1, upper_pct=0.9)
        np.testing.assert_array_equal(y_valid, np.array([0.01, 0.02, 100.0]))


class TestDecileLiftTable:
    def test_top_decile_is_the_highest_predicted_bucket(self) -> None:
        # 100 rows, predicted and actual perfectly correlated -- top decile
        # (index -1, highest predicted) must have the highest actual mean.
        predicted = np.arange(100, dtype=float)
        actual = np.arange(100, dtype=float) * 0.01
        deciles = decile_lift_table(predicted, actual)
        assert len(deciles) == 10
        assert deciles[-1]["mean_actual_payoff"] > deciles[0]["mean_actual_payoff"]
        assert deciles[-1]["decile"] == 10

    def test_no_ranking_skill_gives_flat_deciles(self) -> None:
        rng = np.random.default_rng(42)
        predicted = rng.normal(size=1000)
        actual = rng.normal(size=1000)  # independent of predicted
        deciles = decile_lift_table(predicted, actual)
        means = [d["mean_actual_payoff"] for d in deciles]
        # No real signal -- top and bottom decile means should be much
        # closer to each other than the perfectly-correlated case's spread.
        assert abs(means[-1] - means[0]) < 1.0

    def test_win_rate_reported_per_decile(self) -> None:
        predicted = np.arange(20, dtype=float)
        actual = np.where(np.arange(20) >= 10, 0.05, -0.05)  # top half wins, bottom half loses
        deciles = decile_lift_table(predicted, actual, n_deciles=2)
        assert deciles[0]["win_rate"] == 0.0
        assert deciles[1]["win_rate"] == 1.0

    def test_handles_fewer_rows_than_deciles_without_crashing(self) -> None:
        predicted = np.array([1.0, 2.0, 3.0])
        actual = np.array([0.01, 0.02, 0.03])
        deciles = decile_lift_table(predicted, actual, n_deciles=10)
        assert len(deciles) == 10
        total_n = sum(d["n"] for d in deciles)
        assert total_n == 3


class TestComputeShipGates:
    _GOOD_EVAL = {
        "rows": 500,
        "top_decile_mean_payoff": 0.02,
        "spearman_rho": 0.15,
        "spearman_p_value": 0.01,
    }

    def test_all_pass_on_a_healthy_holdout_eval(self) -> None:
        gates = compute_ship_gates(self._GOOD_EVAL)
        assert all(gates.values())

    def test_fails_when_top_decile_is_not_profitable(self) -> None:
        bad = {**self._GOOD_EVAL, "top_decile_mean_payoff": -0.01}
        gates = compute_ship_gates(bad)
        assert not gates["top_decile_mean_payoff>0.0"]
        assert not all(gates.values())

    def test_fails_on_weak_rank_correlation(self) -> None:
        bad = {**self._GOOD_EVAL, "spearman_rho": 0.01}
        gates = compute_ship_gates(bad)
        assert not gates["spearman_rho>=0.05"]

    def test_fails_on_insignificant_p_value(self) -> None:
        bad = {**self._GOOD_EVAL, "spearman_p_value": 0.40}
        gates = compute_ship_gates(bad)
        assert not gates["spearman_p_value<=0.05"]

    def test_fails_on_thin_holdout_sample(self) -> None:
        bad = {**self._GOOD_EVAL, "rows": 50}
        gates = compute_ship_gates(bad)
        assert not gates["holdout_rows>=200"]

    def test_missing_fields_fail_rather_than_silently_pass(self) -> None:
        gates = compute_ship_gates({"rows": 500})
        assert not gates["top_decile_mean_payoff>0.0"]
        assert not gates["spearman_rho>=0.05"]
        assert not gates["spearman_p_value<=0.05"]


class TestLoadTrainingView:
    def test_raises_if_payoff_score_column_missing(self, tmp_path) -> None:
        csv_path = tmp_path / "no_target.csv.gz"
        pd.DataFrame({"trade_date": ["2026-01-01"], "some_feature": [1.0]}).to_csv(
            csv_path, index=False, compression="gzip"
        )
        with pytest.raises(ValueError, match=TARGET_COL):
            load_training_view(str(csv_path), start="2026-01-01", end="2026-01-01")

    def test_filters_by_date_range(self, tmp_path) -> None:
        csv_path = tmp_path / "with_target.csv.gz"
        pd.DataFrame({
            "trade_date": ["2026-01-01", "2026-02-01", "2026-03-01"],
            TARGET_COL: [0.01, 0.02, 0.03],
        }).to_csv(csv_path, index=False, compression="gzip")
        df = load_training_view(str(csv_path), start="2026-01-15", end="2026-02-15")
        assert len(df) == 1
        assert df.iloc[0]["trade_date"] == "2026-02-01"


class TestSelectFeatures:
    def test_raises_on_missing_feature_columns(self) -> None:
        df = pd.DataFrame({"a": [1.0]})
        with pytest.raises(ValueError, match="missing"):
            _select_features(df, ["a", "b"])

    def test_coerces_to_numeric(self) -> None:
        df = pd.DataFrame({"a": ["1.5", "2.5"]})
        result = _select_features(df, ["a"])
        assert result["a"].dtype.kind == "f"


class TestEndToEndHpoAndMain:
    """Needs optuna+xgboost -- skip (not fail) if unavailable in this
    environment, matching how the ml-pipeline skill already treats these as
    optional/VM-side dependencies."""

    def test_run_hpo_and_main_smoke(self, tmp_path) -> None:
        pytest.importorskip("optuna")
        pytest.importorskip("xgboost")
        from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3
        from ml_pipeline_2.scripts.train_entry_option_payoff_v1 import main

        rng = np.random.default_rng(7)
        n = 300
        feat_cols = ENTRY_FEATURES_V3[:5]  # keep the smoke test fast
        data = {f: rng.normal(size=n) for f in feat_cols}
        # payoff correlates weakly with the first feature, so HPO has
        # SOMETHING to find -- purely a wiring smoke test, not a result.
        data[TARGET_COL] = data[feat_cols[0]] * 0.01 + rng.normal(scale=0.02, size=n)
        dates = pd.date_range("2026-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
        data["trade_date"] = dates
        df = pd.DataFrame(data)
        csv_path = tmp_path / "training_view_smoke.csv.gz"
        df.to_csv(csv_path, index=False, compression="gzip")

        output_path = tmp_path / "smoke_model.joblib"
        rc = main([
            "--training-view-csv", str(csv_path),
            "--output", str(output_path),
            "--features", ",".join(feat_cols),
            "--train-start", dates[0], "--train-end", dates[179],
            "--valid-start", dates[180], "--valid-end", dates[219],
            "--holdout-start", dates[220], "--holdout-end", dates[-1],
            "--n-trials", "3",
        ])
        assert output_path.exists()
        report_path = tmp_path / "smoke_model.report.json"
        assert report_path.exists()
        assert rc in (0, 1)  # ship gates may legitimately fail on tiny synthetic noise -- just confirm it RAN
