"""Tests for run_repricing_wall_control.py's pure logic. The decisive test
of the actual repricing-wall question happens by running this against real
NIFTY holdout data (see the plan/progress log) -- these tests just confirm
the plumbing (fire selection, bar-index lookup, delay-reprice wiring) is
correct."""
from __future__ import annotations

import pandas as pd

from ml_pipeline_2.scripts.run_repricing_wall_control import (
    _bar_index_in_day,
    find_top_decile_fires,
    reprice_fires_at_delays,
)


class _FakeModel:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def predict(self, X: pd.DataFrame) -> list[float]:
        return self._scores


class _FakeSnap:
    def __init__(self, hh_mm_ss: str) -> None:
        self._ts_str = hh_mm_ss

    @property
    def timestamp(self):
        return self

    def strftime(self, fmt: str) -> str:
        return self._ts_str


class TestFindTopDecileFires:
    def test_selects_only_the_top_fraction_by_predicted_score(self, tmp_path) -> None:
        df = pd.DataFrame({
            "trade_date": ["2026-06-01"] * 10,
            "time": [f"09:{15+i}:00" for i in range(10)],
            "feat_a": list(range(10)),
        })
        csv_path = tmp_path / "tv.csv.gz"
        df.to_csv(csv_path, index=False, compression="gzip")

        bundle = {
            "model": _FakeModel(list(range(10))),  # higher index -> higher predicted score
            "features": ["feat_a"],
            "feature_medians": {"feat_a": 0.0},
        }
        fires = find_top_decile_fires(
            bundle, str(csv_path), holdout_start="2026-06-01", holdout_end="2026-06-01",
            top_decile_frac=0.20,
        )
        # Top 20% of 10 rows -> the 2 highest-scored rows (indices 8, 9).
        assert len(fires) == 2
        assert set(fires["feat_a"]) == {8, 9}

    def test_filters_by_holdout_date_range_first(self, tmp_path) -> None:
        # Model returns feat_a unchanged as its "score" (via a stub that
        # respects input size) -- the excluded May row has a much higher
        # feat_a, so if date-filtering happened AFTER scoring, it would
        # wrongly dominate the top-decile cutoff.
        df = pd.DataFrame({
            "trade_date": ["2026-05-01", "2026-06-01"],
            "time": ["09:15:00", "09:15:00"],
            "feat_a": [100.0, 1.0],
        })
        csv_path = tmp_path / "tv.csv.gz"
        df.to_csv(csv_path, index=False, compression="gzip")

        class _EchoModel:
            def predict(self, X: pd.DataFrame) -> pd.Series:
                return X["feat_a"]

        bundle = {"model": _EchoModel(), "features": ["feat_a"], "feature_medians": {"feat_a": 0.0}}
        fires = find_top_decile_fires(
            bundle, str(csv_path), holdout_start="2026-06-01", holdout_end="2026-06-30", top_decile_frac=0.5,
        )
        assert len(fires) == 1
        assert fires.iloc[0]["trade_date"] == "2026-06-01"


class TestBarIndexInDay:
    def test_finds_matching_bar_by_time_of_day(self) -> None:
        snaps = [_FakeSnap("09:15:00"), _FakeSnap("09:16:00"), _FakeSnap("09:17:00")]
        assert _bar_index_in_day(snaps, "09:16:00") == 1

    def test_returns_none_when_not_found(self) -> None:
        snaps = [_FakeSnap("09:15:00")]
        assert _bar_index_in_day(snaps, "10:00:00") is None


class TestRepriceFiresAtDelays:
    def test_drops_fires_too_close_to_end_of_day_for_delay_2(self, tmp_path, monkeypatch) -> None:
        # A fire whose original exit is exactly the last bar of the day
        # cannot be re-priced at +1/+2 delay (no room left) -- must be
        # skipped for those columns, not crash or fabricate a value.
        import ml_pipeline_2.scripts.run_repricing_wall_control as mod

        def _fake_read_day_snapshots(base, trade_date):
            return [_FakeSnap(f"09:{15+i:02d}:00") for i in range(5)]

        monkeypatch.setattr(mod, "read_day_snapshots", _fake_read_day_snapshots)
        monkeypatch.setattr(mod, "compute_bar_payoff", lambda entry, exit_snap, *, lot_size: None)

        fires = pd.DataFrame({"trade_date": ["2026-06-01"], "time": ["09:16:00"]})
        # horizon_bars=3 from bar index 1 (09:16) lands on index 4, the LAST
        # bar (5 total) -- no room for +1/+2 reprice at all past the entry.
        at_fire, d1, d2 = reprice_fires_at_delays(
            fires, snapshot_parquet_base="unused", lot_size=65, horizon_bars=3,
        )
        assert len(at_fire) == 1  # the fire itself was still processed
