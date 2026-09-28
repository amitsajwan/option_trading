"""Tests for study_joint_direction_threshold_banknifty.py. Most of the
grid/consistency/stress-test machinery is imported UNMODIFIED from
study_joint_direction_call_threshold.py (already tested there) -- these
tests focus on what's genuinely new: build_window_record's and
build_window_diagnostics's generalization from a CALL-only script to one
parameterized by --target-class (CALL or PUT), and that the CLI wiring
correctly threads target_class through to the right net-return column and
option leg."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.joint_direction import CALL, CLASS_TO_INT, PUT
from ml_pipeline_2.scripts.study_joint_direction_threshold_banknifty import (
    BANKNIFTY_DEFAULT_HORIZON_BARS,
    BANKNIFTY_DEFAULT_LOT_SIZE,
    LEG_FOR_CLASS,
    NET_RETURN_COL_FOR_CLASS,
    build_window_diagnostics,
)


class TestClassMappings:
    def test_leg_mapping(self) -> None:
        assert LEG_FOR_CLASS[CALL] == "CE"
        assert LEG_FOR_CLASS[PUT] == "PE"

    def test_net_return_column_mapping(self) -> None:
        assert NET_RETURN_COL_FOR_CLASS[CALL] == "net_ce_return"
        assert NET_RETURN_COL_FOR_CLASS[PUT] == "net_pe_return"

    def test_banknifty_defaults_match_verified_labeling(self) -> None:
        assert BANKNIFTY_DEFAULT_HORIZON_BARS == 15
        assert BANKNIFTY_DEFAULT_LOT_SIZE == 30


def _fake_window(window_id: int, *, n: int, proba: np.ndarray, net_return: np.ndarray, argmax_labels: np.ndarray) -> dict:
    return {
        "window": window_id,
        "test_start": f"2026-0{window_id}-01", "test_end": f"2026-0{window_id}-28",
        "n_test_resolved": n,
        "_proba_call": proba,       # holds the TARGET class's proba (see module docstring)
        "_net_ce": net_return,      # holds the TARGET class's actual net return
        "_argmax_labels": argmax_labels,
        "_dte": np.zeros(n),
        "_vix": np.full(n, 12.0),
        "_adx": np.full(n, 25.0),
        "_trade_date": np.array([f"2026-0{window_id}-01"] * n, dtype=object),
    }


class TestBuildWindowDiagnosticsGeneralization:
    def test_raises_on_invalid_target_class(self) -> None:
        with pytest.raises(ValueError):
            build_window_diagnostics([], target_class="NO_TRADE")

    def test_call_uses_call_shaped_keys(self) -> None:
        n = 50
        proba = np.linspace(0, 1, n)
        argmax = np.array([CALL] * n, dtype=object)
        w = _fake_window(1, n=n, proba=proba, net_return=proba * 0.01, argmax_labels=argmax)
        diag = build_window_diagnostics([w], target_class=CALL, top_decile_frac=0.1)
        assert len(diag) == 1
        d = diag[0]
        assert "argmax_call" in d
        assert "top_decile_of_argmax_call" in d
        assert d["argmax_call"]["n"] == n
        assert d["top_decile_of_argmax_call"]["n"] == 5  # top 10% of 50

    def test_put_uses_put_shaped_keys(self) -> None:
        n = 50
        proba = np.linspace(0, 1, n)
        argmax = np.array([PUT] * n, dtype=object)
        w = _fake_window(1, n=n, proba=proba, net_return=proba * 0.01, argmax_labels=argmax)
        diag = build_window_diagnostics([w], target_class=PUT, top_decile_frac=0.1)
        d = diag[0]
        assert "argmax_put" in d
        assert "top_decile_of_argmax_put" in d
        assert "argmax_call" not in d
        assert d["argmax_put"]["n"] == n

    def test_mean_actual_reflects_the_target_classs_own_return_series(self) -> None:
        n = 20
        proba = np.linspace(0, 1, n)
        argmax = np.array([PUT] * n, dtype=object)
        net_return = np.full(n, 0.07)  # distinct, checkable constant
        w = _fake_window(1, n=n, proba=proba, net_return=net_return, argmax_labels=argmax)
        diag = build_window_diagnostics([w], target_class=PUT, top_decile_frac=1.0)
        assert diag[0]["argmax_put"]["mean_actual_return"] == pytest.approx(0.07)
        assert diag[0]["top_decile_of_argmax_put"]["mean_actual_return"] == pytest.approx(0.07)


# ── End-to-end smoke test (needs optuna+xgboost) ────────────────────────────

class TestEndToEndSmoke:
    def _make_synthetic_training_view(self, tmp_path: Path, *, n_days: int, bars_per_day: int) -> Path:
        from ml_pipeline_2.pipeline.direction_margin import CALL_ONLY_FEATURES, PUT_ONLY_FEATURES
        from ml_pipeline_2.scripts.train_joint_direction_v1 import JOINT_FEATURES

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
                row["payoff_score"] = max(row["net_ce_return"], row["net_pe_return"], 0.0) + abs(rng.normal(scale=0.01))
                row["dte"] = float(rng.integers(0, 7))
                row["vix_current"] = float(12.0 + rng.normal(scale=1.0))
                rows.append(row)
        df = pd.DataFrame(rows)
        csv_path = tmp_path / "training_view_banknifty_smoke.csv.gz"
        df.to_csv(csv_path, index=False, compression="gzip")
        return csv_path

    @pytest.mark.parametrize("target_class", [CALL, PUT])
    def test_main_smoke_both_classes(self, tmp_path: Path, target_class: str) -> None:
        pytest.importorskip("optuna")
        pytest.importorskip("xgboost")
        from ml_pipeline_2.scripts.study_joint_direction_threshold_banknifty import main

        csv_path = self._make_synthetic_training_view(tmp_path, n_days=60, bars_per_day=30)
        output_json = tmp_path / f"report_{target_class}.json"
        rc = main([
            "--training-view-csv", str(csv_path),
            "--target-class", target_class,
            "--n-windows", "2", "--min-train-days", "40",
            "--n-trials", "2", "--skip-stress-test",
            "--grid-n-boot", "50", "--grid-n-perm", "50",
            "--final-n-boot", "50", "--final-n-perm", "50",
            "--output-json", str(output_json),
        ])
        assert rc == 0
        report = json.loads(output_json.read_text())
        assert report["config"]["instrument"] == "BANKNIFTY"
        assert report["config"]["target_class"] == target_class
        assert report["config"]["leg"] == LEG_FOR_CLASS[target_class]
        assert "part1_no_filter_grid" in report
        assert "part2_entry_filtered_grid" in report
        assert "window_diagnostics" in report
        assert "headline_candidates" in report
        argmax_key = f"argmax_{target_class.lower()}"
        for d in report["window_diagnostics"]:
            assert argmax_key in d
        assert "XGBClassifier" not in json.dumps(report)
        assert "XGBRegressor" not in json.dumps(report)
