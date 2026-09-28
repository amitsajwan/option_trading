"""Tests for ml_pipeline_2.pipeline.entry_payoff_day_clustering -- the
day-level / cluster-bootstrap statistics used to re-test the entry-payoff
model. Every fixture is synthetic with a hand-computable answer."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.entry_payoff_day_clustering import (
    aggregate_by_day,
    assign_rank_buckets,
    between_day_variance_share,
    bootstrap_mean,
    cluster_bootstrap,
    day_concentration,
    day_mean_score_rho,
    decile_spread,
    demean_by_day,
    early_session_day_frame,
    first_fire_per_day,
    partial_spearman,
    robustness_to_top_days,
    side_agnostic_bucket_table,
    spearman,
    top_contributing_days,
    within_day_bucket_spread,
    within_day_spearman,
    verify_side_returns,
)
from ml_pipeline_2.scripts.train_entry_option_payoff_v1 import decile_lift_table


def _day_frame(days: dict) -> pd.DataFrame:
    """{date: (scores, payoffs)} -> long frame with trade_date/time/score/payoff_score."""
    rows = []
    for d, (scores, pays) in days.items():
        for i, (s, p) in enumerate(zip(scores, pays)):
            rows.append({"trade_date": d, "time": f"{9 + (45 + i) // 60:02d}:{(45 + i) % 60:02d}:00", "score": float(s), "payoff_score": float(p)})
    return pd.DataFrame(rows)


class TestRankBuckets:
    def test_equal_sized_buckets_on_20_rows(self) -> None:
        b = assign_rank_buckets(np.arange(20, dtype=float))
        np.testing.assert_array_equal(b, np.repeat(np.arange(1, 11), 2))

    def test_order_independent_of_input_order(self) -> None:
        rng = np.random.default_rng(0)
        s = rng.permutation(np.arange(30, dtype=float))
        b = assign_rank_buckets(s)
        # highest 3 scores -> bucket 10, lowest 3 -> bucket 1
        assert set(s[b == 10]) == {27.0, 28.0, 29.0}
        assert set(s[b == 1]) == {0.0, 1.0, 2.0}

    def test_matches_original_decile_lift_table(self) -> None:
        rng = np.random.default_rng(1)
        s = rng.normal(size=137)
        y = s + rng.normal(size=137)
        b = assign_rank_buckets(s)
        table = decile_lift_table(s, y)
        for row in table:
            assert row["n"] == int((b == row["decile"]).sum())
            assert row["mean_actual_payoff"] == pytest.approx(float(y[b == row["decile"]].mean()), abs=1e-5)

    def test_empty(self) -> None:
        assert len(assign_rank_buckets(np.array([]))) == 0


class TestDecileSpread:
    def test_known_answer(self) -> None:
        s = np.arange(20, dtype=float)
        y = np.zeros(20)
        y[-2:] = [1.0, 3.0]   # top bucket mean 2
        y[:2] = [-1.0, -1.0]  # bottom bucket mean -1
        top, bot, spread = decile_spread(s, y)
        assert (top, bot, spread) == (2.0, -1.0, 3.0)


class TestSpearman:
    def test_constant_input_is_nan(self) -> None:
        assert np.isnan(spearman(np.ones(10), np.arange(10)))

    def test_perfect(self) -> None:
        assert spearman(np.arange(10), np.arange(10) ** 2) == pytest.approx(1.0)


class TestDayConcentration:
    def test_known_shares_and_counts(self) -> None:
        dates = ["A", "A", "A", "B", "C", "C"]
        pay = np.array([1.0, 1.0, 1.0, 5.0, 0.5, 0.5])  # A=3, B=5, C=1, total 9
        c = day_concentration(dates, pay, top_k=(1, 2, 3))
        assert c.n_fires == 6 and c.n_days == 3
        assert (c.fires_per_day_min, c.fires_per_day_median, c.fires_per_day_max) == (1, 2.0, 3)
        assert c.top_k_share[1] == pytest.approx(5 / 9)
        assert c.top_k_share[2] == pytest.approx(8 / 9)
        assert c.top_k_share[3] == pytest.approx(1.0)
        assert c.top_days[0]["trade_date"] == "B"

    def test_share_above_one_when_other_days_negative(self) -> None:
        c = day_concentration(["A", "B"], np.array([2.0, -1.0]), top_k=(1,))
        assert c.top_k_share[1] == pytest.approx(2.0)

    def test_empty(self) -> None:
        c = day_concentration([], np.array([]))
        assert c.n_fires == 0 and c.n_days == 0

    def test_to_dict_is_json_friendly(self) -> None:
        d = day_concentration(["A"], np.array([1.0])).to_dict()
        assert set(d["top_k_share"]) == {"1", "3", "5"}

    def test_top_contributing_days(self) -> None:
        assert top_contributing_days(["A", "B", "B", "C"], np.array([1.0, 2.0, 2.0, 3.0]), 2) == ["B", "C"]


class TestClusterBootstrap:
    def test_relabels_duplicated_clusters(self) -> None:
        df = _day_frame({f"d{i}": ([0.0] * 5, [float(i)] * 5) for i in range(8)})
        seen = []

        def stat(f: pd.DataFrame) -> float:
            seen.append(f["trade_date"].nunique())
            return 1.0

        cluster_bootstrap(df, stat, n_boot=50, seed=3)
        # first call is the point estimate on the original frame
        assert all(n == 8 for n in seen)

    def test_constant_statistic_ci_collapses(self) -> None:
        df = _day_frame({f"d{i}": ([0.0] * 3, [1.0] * 3) for i in range(6)})
        ci = cluster_bootstrap(df, lambda f: float(f["payoff_score"].mean()), n_boot=100)
        assert ci.point == ci.ci_low == ci.ci_high == 1.0
        assert ci.n_clusters == 6 and ci.frac_boot_le_zero == 0.0

    def test_cluster_ci_much_wider_than_iid_when_bars_identical_within_day(self) -> None:
        # 20 days x 50 identical bars: effective n is 20, not 1000.
        rng = np.random.default_rng(5)
        day_vals = rng.normal(size=20)
        df = _day_frame({f"d{i:02d}": ([0.0] * 50, [v] * 50) for i, v in enumerate(day_vals)})
        mean_stat = lambda f: float(f["payoff_score"].mean())  # noqa: E731
        clus = cluster_bootstrap(df, mean_stat, n_boot=1000, seed=1)
        iid = bootstrap_mean(df["payoff_score"].to_numpy(), n_boot=1000, seed=1)
        day_level = bootstrap_mean(day_vals, n_boot=1000, seed=2)
        clus_w = clus.ci_high - clus.ci_low
        iid_w = iid.ci_high - iid.ci_low
        assert clus_w > 4 * iid_w  # sqrt(50) ~ 7x in expectation
        # cluster bootstrap over equal-size identical-bar days == day-level bootstrap
        assert clus_w == pytest.approx(day_level.ci_high - day_level.ci_low, rel=0.2)

    def test_single_cluster_returns_nan_ci(self) -> None:
        df = _day_frame({"d": ([0.0, 1.0], [1.0, 2.0])})
        ci = cluster_bootstrap(df, lambda f: 1.0, n_boot=10)
        assert np.isnan(ci.ci_low) and ci.n_boot == 0

    def test_excludes_zero_property(self) -> None:
        df = _day_frame({f"d{i}": ([0.0] * 2, [1.0 + i] * 2) for i in range(10)})
        ci = cluster_bootstrap(df, lambda f: float(f["payoff_score"].mean()), n_boot=200)
        assert ci.excludes_zero


class TestBootstrapMean:
    def test_constant(self) -> None:
        ci = bootstrap_mean(np.full(10, 2.0), n_boot=50)
        assert ci.point == ci.ci_low == ci.ci_high == 2.0

    def test_drops_nan_and_short_input(self) -> None:
        ci = bootstrap_mean(np.array([1.0, np.nan]))
        assert ci.point == 1.0 and np.isnan(ci.ci_low)

    def test_symmetric_zero_mean_frac_le_zero_near_half(self) -> None:
        v = np.concatenate([np.ones(50), -np.ones(50)])
        ci = bootstrap_mean(v, n_boot=2000, seed=0)
        assert 0.4 < ci.frac_boot_le_zero < 0.65
        assert ci.ci_low < 0 < ci.ci_high


class TestAggregateByDay:
    def test_known_values(self) -> None:
        df = _day_frame({"A": ([1.0, 3.0], [0.1, 0.3]), "B": ([5.0, 5.0], [1.0, 2.0])})
        df["fire"] = [False, True, False, False]
        out = aggregate_by_day(df, fire_col="fire").set_index("trade_date")
        assert out.loc["A", "mean_score"] == 2.0
        assert out.loc["A", "mean_payoff"] == pytest.approx(0.2)
        assert out.loc["A", "n_fires"] == 1
        assert out.loc["A", "fire_mean_payoff"] == pytest.approx(0.3)
        assert out.loc["A", "nonfire_mean_payoff"] == pytest.approx(0.1)
        assert out.loc["B", "n_fires"] == 0
        assert np.isnan(out.loc["B", "fire_mean_payoff"])
        assert out.loc["B", "nonfire_mean_payoff"] == pytest.approx(1.5)


class TestWithinDay:
    def test_within_day_spearman_signs_and_min_bars(self) -> None:
        x = np.arange(30, dtype=float)
        df = _day_frame({"up": (x, x), "down": (x, -x), "tiny": (x[:5], x[:5])})
        r = within_day_spearman(df, min_bars=20)
        assert set(r.index) == {"up", "down"}
        assert r["up"] == pytest.approx(1.0) and r["down"] == pytest.approx(-1.0)

    def test_pure_between_day_signal_has_zero_within_day_skill(self) -> None:
        # Score is a per-day constant plus noise unrelated to payoff; payoff
        # level also per-day -> huge pooled rho, ~0 within-day rho.
        rng = np.random.default_rng(0)
        days = {}
        for i in range(30):
            days[f"d{i:02d}"] = (i + rng.normal(scale=0.01, size=40), i + rng.normal(scale=0.01, size=40))
        df = _day_frame(days)
        assert spearman(df["score"].to_numpy(), df["payoff_score"].to_numpy()) > 0.95
        assert abs(within_day_spearman(df).mean()) < 0.1
        dm = demean_by_day(df, ["score", "payoff_score"])
        assert abs(spearman(dm["score"].to_numpy(), dm["payoff_score"].to_numpy())) < 0.1
        assert day_mean_score_rho(df) > 0.95

    def test_within_day_bucket_spread(self) -> None:
        x = np.arange(20, dtype=float)
        df = _day_frame({"A": (x, x)})
        s = within_day_bucket_spread(df, min_bars=10)
        # top two payoffs 18,19 mean 18.5; bottom 0,1 mean 0.5
        assert s["A"] == pytest.approx(18.0)

    def test_demean_by_day(self) -> None:
        df = _day_frame({"A": ([1.0, 3.0], [0.0, 0.0]), "B": ([10.0, 20.0], [0.0, 0.0])})
        dm = demean_by_day(df, ["score"])
        np.testing.assert_allclose(dm["score"].to_numpy(), [-1.0, 1.0, -5.0, 5.0])
        assert df["score"].iloc[0] == 1.0  # input untouched


class TestBetweenDayVarianceShare:
    def test_all_variance_between_days(self) -> None:
        assert between_day_variance_share(["A", "A", "B", "B"], np.array([1.0, 1.0, 3.0, 3.0])) == pytest.approx(1.0)

    def test_no_variance_between_days(self) -> None:
        assert between_day_variance_share(["A", "A", "B", "B"], np.array([1.0, 3.0, 1.0, 3.0])) == pytest.approx(0.0)

    def test_constant_is_nan(self) -> None:
        assert np.isnan(between_day_variance_share(["A", "B"], np.array([1.0, 1.0])))


class TestRobustnessToTopDays:
    def test_dropping_the_one_carrying_day_kills_spread(self) -> None:
        rng = np.random.default_rng(2)
        days = {f"d{i:02d}": (rng.normal(size=50), rng.normal(scale=0.01, size=50)) for i in range(9)}
        # "hot" day: highest scores AND big payoffs -> owns the whole top decile
        days["hot"] = (10 + rng.normal(size=50), 5 + rng.normal(scale=0.01, size=50))
        df = _day_frame(days)
        rows = robustness_to_top_days(df, ks=(0, 1))
        assert rows[0]["k_dropped"] == 0 and rows[0]["n_days"] == 10
        base_top, base_bot, base_spread = decile_spread(df["score"].to_numpy(), df["payoff_score"].to_numpy())
        assert rows[0]["spread"] == pytest.approx(base_spread, abs=1e-4)
        assert rows[0]["spread"] > 4.0
        assert rows[1]["dropped_days"] == ["hot"]
        assert rows[1]["n_days"] == 9
        assert abs(rows[1]["spread"]) < 0.05


class TestEarlySessionDayFrame:
    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "trade_date": ["A"] * 4 + ["B"] * 2 + ["C"] * 2,
            "time": ["09:45:00", "10:15:00", "10:16:00", "11:00:00", "09:45:00", "10:00:00", "10:30:00", "11:00:00"],
            "score": [1.0, 3.0, 100.0, 100.0, 5.0, 7.0, 9.0, 9.0],
            "payoff_score": [99.0, 99.0, 0.2, 0.4, 1.0, 1.0, 0.5, 0.7],
            "vix": [10.0, 11.0, 12.0, 13.0, 20.0, 21.0, 30.0, 31.0],
        })

    def test_split_at_cutoff_inclusive_early(self) -> None:
        ef = early_session_day_frame(self._frame(), cutoff="10:15:00", early_mean_cols=("vix",), at_cutoff_cols=("vix",)).set_index("trade_date")
        # B has no later bars, C has no early bars -> both dropped
        assert list(ef.index) == ["A"]
        a = ef.loc["A"]
        assert a["early_mean_score"] == 2.0 and a["early_max_score"] == 3.0
        assert a["later_mean_payoff"] == pytest.approx(0.3)  # early-bar payoffs (99) excluded
        assert a["early_mean_vix"] == 10.5
        assert a["at_cutoff_vix"] == 11.0  # last bar at/before cutoff
        assert a["n_early"] == 2 and a["n_later"] == 2


class TestPartialSpearman:
    def test_common_driver_is_partialled_out(self) -> None:
        rng = np.random.default_rng(0)
        z = rng.normal(size=400)
        x = z + 0.3 * rng.normal(size=400)
        y = z + 0.3 * rng.normal(size=400)
        assert spearman(x, y) > 0.8
        assert abs(partial_spearman(x, y, z)) < 0.12

    def test_signal_beyond_control_survives(self) -> None:
        rng = np.random.default_rng(1)
        z = rng.normal(size=400)
        u = rng.normal(size=400)
        x = z + u
        y = z + u + 0.2 * rng.normal(size=400)
        assert partial_spearman(x, y, z) > 0.8

    def test_multi_column_controls_and_nan_rows(self) -> None:
        # A shared driver in column 0 plus an irrelevant column 1: the extra
        # control must not stop the driver being partialled out, and a NaN
        # row must be dropped rather than poisoning the fit. (Note: a SUM of
        # several controls is only partly removed by any rank-based partial
        # correlation, since rank(z1+z2) is not linear in rank(z1), rank(z2).)
        rng = np.random.default_rng(2)
        driver = rng.normal(size=400)
        z = np.column_stack([driver, rng.normal(size=400)])
        x = driver + 0.3 * rng.normal(size=400)
        y = driver + 0.3 * rng.normal(size=400)
        assert spearman(x, y) > 0.8
        x[0] = np.nan
        z[1, 1] = np.inf
        assert abs(partial_spearman(x, y, z)) < 0.12


class TestFirstFirePerDay:
    def test_earliest_bar_over_threshold(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["A", "A", "A", "B", "B"],
            "time": ["10:00:00", "09:50:00", "11:00:00", "09:45:00", "09:46:00"],
            "score": [0.9, 0.2, 0.95, 0.1, 0.2],
            "payoff_score": [0.5, 0.1, 0.3, 1.0, 1.0],
        })
        ff = first_fire_per_day(df, threshold=0.5)
        assert list(ff["trade_date"]) == ["A"]  # B never fires
        assert ff.loc[0, "time"] == "10:00:00"
        assert ff.loc[0, "payoff_score"] == 0.5
        assert ff.loc[0, "day_mean_payoff"] == pytest.approx(0.3)


class TestSideAgnostic:
    def test_verify_accepts_matching_and_rejects_other_horizon(self) -> None:
        ce = np.array([0.1, -0.2, 0.3, np.nan])
        pe = np.array([-0.1, 0.05, -0.3, 0.2])
        assert verify_side_returns(np.maximum(ce, pe), ce, pe) == 0.0  # NaN row ignored
        with pytest.raises(ValueError, match="different horizon"):
            verify_side_returns(np.array([0.5, 0.5, 0.5, 0.5]), ce, pe)

    def test_verify_no_finite_rows_raises(self) -> None:
        with pytest.raises(ValueError):
            verify_side_returns(np.array([np.nan]), np.array([np.nan]), np.array([np.nan]))

    def test_bucket_table_known_answer(self) -> None:
        # Top bucket: (ce, pe) = (+1, -1) -> hindsight +1, coin flip 0.
        s = np.arange(20, dtype=float)
        ce = np.zeros(20)
        pe = np.zeros(20)
        ce[-2:], pe[-2:] = 1.0, -1.0
        ce[:2], pe[:2] = -0.2, -0.4
        t = side_agnostic_bucket_table(s, ce, pe)
        assert t[-1] == {"bucket": 10, "n": 2, "hindsight_max": 1.0, "coin_flip": 0.0, "ce_only": 1.0, "pe_only": -1.0}
        assert t[0]["hindsight_max"] == -0.2 and t[0]["coin_flip"] == pytest.approx(-0.3)

    def test_bucket_table_excludes_nan_rows(self) -> None:
        s = np.arange(10, dtype=float)
        ce = np.ones(10)
        ce[9] = np.nan
        t = side_agnostic_bucket_table(s, ce, np.ones(10), n_buckets=2)
        assert t[1]["n"] == 4
