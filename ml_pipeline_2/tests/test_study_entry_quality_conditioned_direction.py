"""Tests for ml_pipeline_2.scripts.study_entry_quality_conditioned_direction's
pure/testable helper functions. The full walk-forward `main()` needs real
NIFTY history and is exercised separately against real data, not here (per
this project's convention -- see build_direction_meta_dataset.py's own tests
for the same split between "unit-testable helper" and "real-data-only
orchestration")."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.conditional_direction import WalkForwardWindow
from ml_pipeline_2.scripts.study_entry_quality_conditioned_direction import (
    best_signal_in_top_deciles,
    pool_decile_tables,
    recompute_winning_side_at_delay,
    run_conditioned_vs_unconditioned,
    select_window_frames,
)

xgb = pytest.importorskip("xgboost")


class TestSelectWindowFrames:
    def test_splits_by_date_range_with_no_overlap(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"],
            "v": [1, 2, 3, 4, 5],
        })
        window = WalkForwardWindow(1, "2026-01-01", "2026-01-03", "2026-01-04", "2026-01-05")
        train_df, test_df = select_window_frames(df, window)
        assert list(train_df["v"]) == [1, 2, 3]
        assert list(test_df["v"]) == [4, 5]


class TestPoolDecileTables:
    def test_combines_counts_across_ok_windows_only(self) -> None:
        window_results = [
            {"status": "ok", "decile_table": [
                {"signal": "momentum_5m", "decile": 1, "n_total": 10, "n_voted": 10, "n_correct": 5, "coverage": 1.0, "accuracy": 0.5},
                {"signal": "momentum_5m", "decile": 10, "n_total": 10, "n_voted": 10, "n_correct": 9, "coverage": 1.0, "accuracy": 0.9},
            ]},
            {"status": "ok", "decile_table": [
                {"signal": "momentum_5m", "decile": 1, "n_total": 10, "n_voted": 10, "n_correct": 4, "coverage": 1.0, "accuracy": 0.4},
                {"signal": "momentum_5m", "decile": 10, "n_total": 10, "n_voted": 10, "n_correct": 8, "coverage": 1.0, "accuracy": 0.8},
            ]},
            {"status": "thin_sample"},  # must be excluded entirely
        ]
        pooled = pool_decile_tables(window_results)
        by_decile = {row["decile"]: row for row in pooled}
        assert by_decile[1]["n_voted"] == 20
        assert by_decile[1]["n_correct"] == 9
        assert by_decile[1]["accuracy"] == pytest.approx(9 / 20)
        assert by_decile[10]["accuracy"] == pytest.approx(17 / 20)

    def test_empty_input_returns_empty_list(self) -> None:
        assert pool_decile_tables([]) == []


class TestBestSignalInTopDeciles:
    def test_picks_highest_accuracy_signal_meeting_min_voted(self) -> None:
        pooled = [
            {"signal": "momentum_5m", "decile": 9, "n_voted": 50, "n_correct": 30, "coverage": 1.0, "accuracy": 0.6, "n_total": 50},
            {"signal": "momentum_5m", "decile": 10, "n_voted": 50, "n_correct": 30, "coverage": 1.0, "accuracy": 0.6, "n_total": 50},
            {"signal": "vix_chg", "decile": 9, "n_voted": 50, "n_correct": 40, "coverage": 1.0, "accuracy": 0.8, "n_total": 50},
            {"signal": "vix_chg", "decile": 10, "n_voted": 50, "n_correct": 40, "coverage": 1.0, "accuracy": 0.8, "n_total": 50},
            {"signal": "composite_proxy", "decile": 10, "n_voted": 50, "n_correct": 49, "coverage": 1.0, "accuracy": 0.98, "n_total": 50},
        ]
        best = best_signal_in_top_deciles(pooled, top_deciles=(9, 10))
        assert best == "vix_chg"  # composite_proxy excluded by design

    def test_signal_below_min_voted_is_ignored(self) -> None:
        pooled = [
            {"signal": "thin_signal", "decile": 10, "n_voted": 5, "n_correct": 5, "coverage": 1.0, "accuracy": 1.0, "n_total": 5},
        ]
        assert best_signal_in_top_deciles(pooled, top_deciles=(9, 10), min_voted=30) is None

    def test_no_eligible_signal_returns_none(self) -> None:
        assert best_signal_in_top_deciles([], top_deciles=(9, 10)) is None


class TestRunConditionedVsUnconditioned:
    def _synthetic_split(self, n: int = 400, seed: int = 0):
        rng = np.random.default_rng(seed)
        f1 = rng.normal(size=n)
        f2 = rng.normal(size=n)
        payoff = rng.uniform(0.0, 1.0, size=n)
        side = np.where(f1 > 0, "CE", "PE")
        df = pd.DataFrame({
            "f1": f1, "f2": f2, "payoff_score": payoff, "payoff_winning_side": side,
            "trade_date": "2026-01-01", "bar_index": np.arange(n),
        })
        return df

    def test_reports_ok_status_and_both_accuracies_with_enough_data(self) -> None:
        train_df = self._synthetic_split(n=400, seed=0)
        test_df = self._synthetic_split(n=200, seed=1)
        # Fake deciles: everyone in decile 10 (i.e. "everything is top decile")
        # so both train/test top-decile subsets are non-trivial in size.
        train_deciles = np.full(len(train_df), 10)
        test_deciles = np.full(len(test_df), 10)
        result = run_conditioned_vs_unconditioned(
            train_df, test_df, ["f1", "f2"], train_deciles, test_deciles, top_deciles=(9, 10),
        )
        assert result["status"] == "ok"
        assert 0.0 <= result["acc_unconditioned_all_bars"] <= 1.0
        assert 0.0 <= result["acc_conditioned_top_decile"] <= 1.0
        assert "comparison_conditioned_minus_unconditioned" in result

    def test_thin_top_decile_train_sample_reports_status_without_fitting_model_b(self) -> None:
        train_df = self._synthetic_split(n=400, seed=0)
        test_df = self._synthetic_split(n=200, seed=1)
        train_deciles = np.full(len(train_df), 1)
        train_deciles[:5] = 10  # only 5 "top decile" train rows -- too thin
        test_deciles = np.full(len(test_df), 10)
        result = run_conditioned_vs_unconditioned(
            train_df, test_df, ["f1", "f2"], train_deciles, test_deciles, top_deciles=(9, 10), min_rows=30,
        )
        assert result["status"] == "thin_top_decile_train_sample"
        assert "acc_conditioned_top_decile" not in result

    def test_no_top_decile_test_bars_reports_that_status(self) -> None:
        train_df = self._synthetic_split(n=100, seed=0)
        test_df = self._synthetic_split(n=100, seed=1)
        train_deciles = np.full(len(train_df), 10)
        test_deciles = np.full(len(test_df), 1)  # nothing in decile 9/10
        result = run_conditioned_vs_unconditioned(
            train_df, test_df, ["f1", "f2"], train_deciles, test_deciles, top_deciles=(9, 10),
        )
        assert result["status"] == "no_top_decile_test_bars"


class TestRecomputeWinningSideAtDelay:
    def _write_synthetic_day(self, tmp_path: Path) -> Path:
        base = tmp_path / "parquet_data"
        day_dir = base / "snapshots" / "trade_date=2026-01-05"
        day_dir.mkdir(parents=True)
        payloads = []
        for i in range(30):
            ts = pd.Timestamp("2026-01-05 04:15:00") + pd.Timedelta(minutes=i)
            payloads.append({
                "snapshot_id": f"s{i}",
                "timestamp": ts.isoformat() + "+00:00",
                "trade_date": "2026-01-05",
                "chain_aggregates": {"atm_strike": 24500},
                "atm_options": {
                    "atm_ce_close": 100.0 + i * 2.0, "atm_pe_close": max(5.0, 100.0 - i * 2.0),
                    "atm_ce_oi": 50000.0, "atm_pe_oi": 50000.0,
                },
                "strikes": [{"strike": 24500, "ce_ltp": 100.0 + i * 2.0, "pe_ltp": max(5.0, 100.0 - i * 2.0)}],
            })
        rows = [{"snapshot_raw_json": json.dumps(p)} for p in payloads]
        pd.DataFrame(rows).to_parquet(day_dir / "data.parquet", index=False)
        return base

    def test_recomputes_winning_side_using_delayed_entry_same_exit(self, tmp_path: Path) -> None:
        base = self._write_synthetic_day(tmp_path)
        rows = pd.DataFrame({"trade_date": ["2026-01-05"], "bar_index": [0], "payoff_winning_side": ["CE"]})
        out = recompute_winning_side_at_delay(rows, str(base), delay_bars=2, horizon_bars=10, lot_size=65)
        assert out.iloc[0] == "CE"  # still a rallying day -- CE still wins after a 2-bar-delayed entry

    def test_missing_day_leaves_none(self, tmp_path: Path) -> None:
        base = tmp_path / "empty_parquet_data"
        (base / "snapshots").mkdir(parents=True)
        rows = pd.DataFrame({"trade_date": ["2099-01-01"], "bar_index": [0], "payoff_winning_side": ["CE"]})
        out = recompute_winning_side_at_delay(rows, str(base), delay_bars=1, horizon_bars=5, lot_size=65)
        assert out.iloc[0] is None
