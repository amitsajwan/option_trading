"""Tests for train_joint_direction_banknifty_v1.py. This script is a thin
BankNifty-specific CLI wrapper around train_joint_direction_v1's already
fully-tested, instrument-agnostic pure functions (evaluate_window,
pool_window_results, class_verdict, run_class_stress_test, JOINT_FEATURES --
see test_train_joint_direction_v1.py and test_joint_direction.py for their
own dedicated tests, not repeated here). What's genuinely new here and needs
its own coverage: the BankNifty-specific CLI defaults, that BankNifty's own
walk-forward windows are built fresh (not NIFTY's), and that the track-5
cross-track comparison is correctly omitted (not fabricated) end-to-end.
HPO/main() end-to-end tests need optuna+xgboost (skipped gracefully via
importorskip, matching every sibling script's own convention)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.direction_margin import CALL_ONLY_FEATURES, PUT_ONLY_FEATURES
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3
from ml_pipeline_2.scripts.train_joint_direction_banknifty_v1 import (
    BANKNIFTY_DEFAULT_HORIZON_BARS,
    BANKNIFTY_DEFAULT_LOT_SIZE,
    BANKNIFTY_DEFAULT_PARQUET_BASE,
    BANKNIFTY_DEFAULT_TRAINING_VIEW,
    JOINT_FEATURES,
)


class TestBankNiftyDefaults:
    def test_defaults_point_at_banknifty_not_nifty(self) -> None:
        assert "banknifty" in BANKNIFTY_DEFAULT_TRAINING_VIEW.lower()
        assert "nifty" not in BANKNIFTY_DEFAULT_PARQUET_BASE.lower().replace("banknifty", "")
        assert "banknifty" in BANKNIFTY_DEFAULT_PARQUET_BASE.lower()

    def test_lot_size_and_horizon_match_the_banknifty_entry_timing_view(self) -> None:
        # Verified 2026-09-28 by a real spot-check against
        # training_view_banknifty_option_pnl.csv.gz's own payoff_score/
        # payoff_winning_side columns (see module docstring): horizon_bars=15
        # reproduced them exactly (321/321 matched on a full sample day,
        # correctly offset by the base view's own 30-bar AM-warmup skip);
        # horizon_bars=5 (BankNifty's UNRELATED live model's label_horizon_min)
        # did not (0/321 matched). Locked here so a future edit can't silently
        # drift this script back to the wrong horizon.
        assert BANKNIFTY_DEFAULT_HORIZON_BARS == 15
        assert BANKNIFTY_DEFAULT_LOT_SIZE == 30

    def test_reexports_the_same_joint_features_as_nifty_track(self) -> None:
        # The feature SET is architecture, not instrument-specific -- must
        # stay identical to train_joint_direction_v1's own JOINT_FEATURES,
        # not a re-derived (and possibly drifted) copy.
        assert set(ENTRY_FEATURES_V3).issubset(set(JOINT_FEATURES))
        for f in CALL_ONLY_FEATURES:
            assert f in JOINT_FEATURES
        for f in PUT_ONLY_FEATURES:
            assert f in JOINT_FEATURES


class TestEndToEndHpoAndMain:
    """Needs optuna+xgboost -- skip (not fail) if unavailable. Purely a
    wiring smoke test on tiny synthetic data."""

    def _make_synthetic_training_view(self, tmp_path: Path, *, n_days: int, bars_per_day: int) -> Path:
        rng = np.random.default_rng(11)
        dates = pd.date_range("2026-01-01", periods=n_days, freq="D").strftime("%Y-%m-%d")
        rows = []
        for d in dates:
            for b in range(bars_per_day):
                row = {"trade_date": d, "bar_index": b}
                for f in JOINT_FEATURES:
                    if f not in row:
                        row[f] = rng.normal()
                row["net_ce_return"] = row[CALL_ONLY_FEATURES[0]] * 0.01 + rng.normal(scale=0.02)
                row["net_pe_return"] = row[PUT_ONLY_FEATURES[0]] * 0.01 + rng.normal(scale=0.02)
                rows.append(row)
        df = pd.DataFrame(rows)
        csv_path = tmp_path / "training_view_banknifty_smoke.csv.gz"
        df.to_csv(csv_path, index=False, compression="gzip")
        return csv_path

    def test_main_smoke(self, tmp_path: Path) -> None:
        pytest.importorskip("optuna")
        pytest.importorskip("xgboost")
        from ml_pipeline_2.scripts.train_joint_direction_banknifty_v1 import main

        csv_path = self._make_synthetic_training_view(tmp_path, n_days=60, bars_per_day=30)
        output_json = tmp_path / "report.json"
        rc = main([
            "--training-view-csv", str(csv_path),
            "--n-windows", "2", "--min-train-days", "40",
            "--n-trials", "2", "--skip-stress-test",
            "--output-json", str(output_json),
        ])
        assert rc == 0
        assert output_json.exists()
        report = json.loads(output_json.read_text())
        assert report["config"]["instrument"] == "BANKNIFTY"
        assert "windows" in report
        assert "pooled" in report
        assert len(report["windows"]) == 2
        assert "overall_class_balance" in report
        assert "headline" in report
        for key in ("call_verdict", "put_verdict", "no_trade_verdict", "any_class_ship_gates_pass"):
            assert key in report["headline"]
        # No track-5-style cross-track baseline comparison for BankNifty --
        # see module docstring for why (that track had no published number
        # yet). Must be ABSENT, not present-with-a-fabricated-value.
        assert "call_vs_track5_independent_regressor" not in report["headline"]
        assert "put_vs_track5_independent_regressor" not in report["headline"]
        # --skip-stress-test was passed -- neither stress test should have run.
        assert report["headline"]["call_stress_test"] is None
        assert report["headline"]["put_stress_test"] is None
        # No raw model/medians objects leaked into the JSON.
        for w in report["windows"]:
            if "model_meta" in w:
                assert "model" not in w["model_meta"]
                assert "medians" not in w["model_meta"]
            assert "XGBClassifier" not in json.dumps(w)

    def test_windows_built_fresh_against_the_dataframes_own_date_range(self, tmp_path: Path) -> None:
        """Confirms this script calls build_expanding_windows against
        WHATEVER date range its own input carries (task requirement: do not
        reuse NIFTY's exact date boundaries) -- using a date range that
        starts well after NIFTY's real 2024-11-04 history to prove the
        windows are derived from the data, not hardcoded."""
        pytest.importorskip("optuna")
        pytest.importorskip("xgboost")
        from ml_pipeline_2.scripts.train_joint_direction_banknifty_v1 import main

        csv_path = self._make_synthetic_training_view(tmp_path, n_days=60, bars_per_day=10)
        # Shift dates far into a range NIFTY's real history never touches.
        df = pd.read_csv(csv_path)
        shifted_dates = pd.date_range("2030-01-01", periods=df["trade_date"].nunique(), freq="D").strftime("%Y-%m-%d")
        mapping = dict(zip(sorted(df["trade_date"].unique()), shifted_dates))
        df["trade_date"] = df["trade_date"].map(mapping)
        df.to_csv(csv_path, index=False, compression="gzip")

        output_json = tmp_path / "report_shifted.json"
        rc = main([
            "--training-view-csv", str(csv_path),
            "--n-windows", "2", "--min-train-days", "40",
            "--n-trials", "2", "--skip-stress-test",
            "--output-json", str(output_json),
        ])
        assert rc == 0
        report = json.loads(output_json.read_text())
        for w in report["windows"]:
            assert w["train_start"].startswith("2030-")
