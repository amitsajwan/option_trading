"""Tests for scripts/study_entry_payoff_day_clustering.py: derived baseline
columns, the inner-validation split, and synthetic end-to-end runs of the
analysis suite and the walk-forward driver (the latter needs xgboost)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.scripts.study_entry_payoff_day_clustering import (
    SCORE_COL,
    TARGET_COL,
    add_derived_columns,
    analyze_scored_frame,
    attach_side_returns,
    run_walkforward,
    split_inner_valid,
)


def _synthetic(n_days: int = 24, bars: int = 40, seed: int = 0) -> pd.DataFrame:
    """Day-level "volatility" v_d drives both a feature and every bar's
    payoff on that day; within a day, payoff is pure noise -- i.e. a pure
    between-day signal with zero within-day skill by construction."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-01", periods=n_days).strftime("%Y-%m-%d")
    rows = []
    for i, d in enumerate(dates):
        v = rng.gamma(2.0, 0.05)
        dte = i % 5
        for b in range(bars):
            m = 45 + b
            rows.append({
                "trade_date": d, "time": f"{9 + m // 60:02d}:{m % 60:02d}:00",
                "f1": v + rng.normal(scale=0.005), "f2": rng.normal(),
                "dte": dte, "vel_price_delta_open": rng.normal(),
                "atr_14_1m": v, "range_30": v, "bb_width_20": rng.normal(), "vix_current": 12 + v,
                TARGET_COL: v + rng.normal(scale=0.01),
            })
    return pd.DataFrame(rows)


class TestAddDerivedColumns:
    def test_prior_day_payoff_is_previous_trading_day_mean(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["2025-01-01", "2025-01-01", "2025-01-02", "2025-01-03"],
            "time": ["09:45:00"] * 4, "dte": [3, 3, 2, 1], "vel_price_delta_open": [-5.0, 2.0, 0.0, 1.0],
            TARGET_COL: [0.1, 0.3, 0.5, 0.9],
        })
        out = add_derived_columns(df)
        assert np.isnan(out.loc[0, "prior_day_mean_payoff"])
        assert out.loc[2, "prior_day_mean_payoff"] == pytest.approx(0.2)
        assert out.loc[3, "prior_day_mean_payoff"] == pytest.approx(0.5)
        assert list(out["neg_dte"]) == [-3, -3, -2, -1]
        assert out.loc[0, "abs_move_from_open"] == 5.0
        assert "prior_day_mean_payoff" not in df.columns  # input untouched


class TestSplitInnerValid:
    def test_last_n_days_become_valid(self) -> None:
        df = _synthetic(n_days=10, bars=2)
        fit, valid = split_inner_valid(df, 3)
        days = sorted(df["trade_date"].unique())
        assert sorted(valid["trade_date"].unique()) == days[-3:]
        assert sorted(fit["trade_date"].unique()) == days[:-3]
        assert fit["trade_date"].max() < valid["trade_date"].min()

    def test_too_few_days_raises(self) -> None:
        with pytest.raises(ValueError):
            split_inner_valid(_synthetic(n_days=3, bars=2), 3)


class TestAnalyzeScoredFrame:
    def test_pure_between_day_signal_is_diagnosed_as_such(self) -> None:
        df = add_derived_columns(_synthetic())
        df[SCORE_COL] = df["f1"]  # a perfect "which day" detector, no within-day skill
        res = analyze_scored_frame(df, n_boot=100, fire_threshold=float(df[SCORE_COL].quantile(0.9)))
        assert res["bar_level"]["spearman_rho"] > 0.5
        assert res["day_level"]["rho_day_mean_score_vs_day_mean_payoff"] > 0.9
        assert abs(res["within_day"]["mean_per_day_rho"]["point"]) < 0.15
        assert res["decomposition"]["payoff_between_day_variance_share"] > 0.9
        # the top decile lives on a handful of days
        assert res["concentration"]["n_days"] <= 4
        assert [r["k_dropped"] for r in res["top_day_removal"]] == [0, 1, 3, 5]
        assert len(res["early_session"]) == 2
        assert res["early_session"][0]["rho_early_mean_score_vs_later_payoff"] > 0.8
        assert "model_score" in res["baseline_rankers"]
        assert res["first_fire_per_day"]["n_days_fired"] >= 1
        assert "bar_partial_rho_given_dte" in res["dte_control"]


class TestRunWalkforward:
    def test_end_to_end_small(self) -> None:
        pytest.importorskip("xgboost")
        df = add_derived_columns(_synthetic(n_days=40, bars=30, seed=1))
        out = run_walkforward(df, features=["f1", "f2"], n_windows=2, min_train_days=24,
                              inner_valid_days=5, hpo_trials=0, n_boot=30)
        assert len(out["windows"]) == 2
        w1, w2 = out["windows"]
        # no leakage: every window's test starts after its fit and valid spans
        for w in (w1, w2):
            assert w["window"]["fit_end"] < w["window"]["valid_start"] <= w["window"]["valid_end"] < w["window"]["test_start"]
        assert w2["window"]["test_start"] > w1["window"]["test_end"]
        assert out["pooled"]["bar_level"]["n_days"] == 16
        assert out["config"]["hpo_trials"] == 0


class TestAttachSideReturns:
    def _base(self) -> pd.DataFrame:
        return pd.DataFrame({"trade_date": ["2025-01-01"] * 2, "time": ["09:45:00", "09:46:00"], TARGET_COL: [0.3, 0.1]})

    def test_join_and_verify(self, tmp_path) -> None:
        side = pd.DataFrame({"trade_date": ["2025-01-01"] * 2, "time": ["09:46:00", "09:45:00"],
                             "net_ce_return": [0.1, -0.2], "net_pe_return": [-0.5, 0.3]})
        p = tmp_path / "side.csv"
        side.to_csv(p, index=False)
        out = attach_side_returns(self._base(), str(p))
        assert list(out["net_pe_return"]) == [0.3, -0.5]

    def test_wrong_horizon_file_raises(self, tmp_path) -> None:
        side = pd.DataFrame({"trade_date": ["2025-01-01"] * 2, "time": ["09:45:00", "09:46:00"],
                             "net_ce_return": [0.9, 0.9], "net_pe_return": [0.0, 0.0]})
        p = tmp_path / "side.csv"
        side.to_csv(p, index=False)
        with pytest.raises(ValueError):
            attach_side_returns(self._base(), str(p))

    def test_analyze_emits_side_agnostic_section(self) -> None:
        df = add_derived_columns(_synthetic(n_days=12, bars=30))
        df["net_ce_return"] = df[TARGET_COL]
        df["net_pe_return"] = df[TARGET_COL] - 0.5
        df[SCORE_COL] = df["f1"]
        res = analyze_scored_frame(df, n_boot=20, cutoffs=("10:00:00",))
        sa = res["side_agnostic"]
        assert len(sa["bucket_table"]) == 10
        top = sa["bucket_table"][-1]
        assert top["coin_flip"] == pytest.approx(top["hindsight_max"] - 0.25, abs=1e-4)
