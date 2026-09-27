"""Tests for study_joint_direction_call_threshold.py. The pure grid/
diagnostics glue (against synthetic in-memory "window" dicts) is tested
unconditionally; the full HPO/main() end-to-end wiring needs optuna+xgboost
and is skipped gracefully via importorskip, matching every sibling
direction-research script's own test convention."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.joint_direction import CALL, CLASS_TO_INT
from ml_pipeline_2.scripts.study_joint_direction_call_threshold import (
    _find_consistent_candidates,
    build_window_diagnostics,
    run_mask_stress_test,
    run_threshold_grid,
)


def _fake_window(window_id: int, *, n: int, proba_call: np.ndarray, net_ce: np.ndarray, argmax_labels=None, dte=None, vix=None, adx=None, trade_date=None, entry_score=None) -> dict:
    return {
        "window": window_id,
        "test_start": f"2026-0{window_id}-01", "test_end": f"2026-0{window_id}-28",
        "n_test_resolved": n,
        "_proba_call": proba_call,
        "_net_ce": net_ce,
        "_net_pe": -net_ce,  # arbitrary, unused by these tests
        "_argmax_labels": argmax_labels if argmax_labels is not None else np.array([CALL] * n, dtype=object),
        "_dte": dte if dte is not None else np.zeros(n),
        "_vix": vix if vix is not None else np.full(n, 12.0),
        "_adx": adx if adx is not None else np.full(n, 25.0),
        "_trade_date": trade_date if trade_date is not None else np.array([f"2026-0{window_id}-01"] * n, dtype=object),
        "_entry_score": entry_score if entry_score is not None else np.linspace(0, 1, n),
    }


class TestRunThresholdGrid:
    def test_consistent_positive_signal_across_windows(self) -> None:
        rng = np.random.default_rng(0)
        windows = []
        for wid in range(1, 4):
            n = 300
            proba = rng.uniform(0, 1, n)
            net_ce = proba * 0.1 + rng.normal(0, 0.01, n)  # real, positive relationship
            windows.append(_fake_window(wid, n=n, proba_call=proba, net_ce=net_ce))
        thresholds = [0.5, 0.9]
        grid = run_threshold_grid(windows, thresholds, min_rows=5, n_boot=200, n_perm=200)
        assert len(grid) == 2
        high_thr_row = grid[1]
        assert high_thr_row["consistency"]["all_windows_positive"] is True

    def test_inconsistent_signal_flagged_not_all_positive(self) -> None:
        windows = [
            _fake_window(1, n=100, proba_call=np.full(100, 0.9), net_ce=np.full(100, 0.05)),
            _fake_window(2, n=100, proba_call=np.full(100, 0.9), net_ce=np.full(100, -0.02)),
            _fake_window(3, n=100, proba_call=np.full(100, 0.9), net_ce=np.full(100, -0.01)),
        ]
        grid = run_threshold_grid(windows, [0.5], min_rows=5, n_boot=100, n_perm=100)
        c = grid[0]["consistency"]
        assert c["all_windows_positive"] is False
        assert c["n_positive_windows"] == 1

    def test_base_masks_restrict_each_window_independently(self) -> None:
        n = 100
        w1 = _fake_window(1, n=n, proba_call=np.full(n, 0.9), net_ce=np.concatenate([np.full(50, 1.0), np.full(50, -1.0)]))
        mask1 = np.array([True] * 50 + [False] * 50)
        grid = run_threshold_grid([w1], [0.5], base_masks=[mask1], min_rows=5, n_boot=50, n_perm=50)
        assert grid[0]["pooled"]["mean_actual"] == pytest.approx(1.0)

    def test_recent_n_reflected_in_consistency(self) -> None:
        windows = [
            _fake_window(1, n=50, proba_call=np.full(50, 0.9), net_ce=np.full(50, -0.5)),
            _fake_window(2, n=50, proba_call=np.full(50, 0.9), net_ce=np.full(50, 0.1)),
            _fake_window(3, n=50, proba_call=np.full(50, 0.9), net_ce=np.full(50, 0.1)),
        ]
        grid = run_threshold_grid(windows, [0.5], recent_n=2, min_rows=5, n_boot=50, n_perm=50)
        c = grid[0]["consistency"]
        assert c["all_windows_positive"] is False
        assert c["recent_all_positive"] is True


class TestFindConsistentCandidates:
    def test_filters_to_consistent_rows_only(self) -> None:
        rows = [
            {"threshold": 0.5, "consistency": {"all_windows_positive": False, "recent_all_positive": False}},
            {"threshold": 0.9, "consistency": {"all_windows_positive": True, "recent_all_positive": True}},
            {"threshold": 0.95, "consistency": {"all_windows_positive": False, "recent_all_positive": True}},
        ]
        out = _find_consistent_candidates(rows)
        assert [r["threshold"] for r in out] == [0.9, 0.95]

    def test_empty_when_none_consistent(self) -> None:
        rows = [{"threshold": 0.5, "consistency": {"all_windows_positive": False, "recent_all_positive": False}}]
        assert _find_consistent_candidates(rows) == []


class TestBuildWindowDiagnostics:
    def test_reports_regime_and_concentration(self) -> None:
        n = 100
        proba = np.linspace(0, 1, n)
        argmax = np.array([CALL] * n, dtype=object)
        dte = np.array([0.0] * 10 + [3.0] * 90)
        trade_date = np.array(["2026-01-01"] * 10 + [f"2026-02-{i:02d}" for i in range(1, 91)], dtype=object)
        w = _fake_window(1, n=n, proba_call=proba, net_ce=proba * 0.01, argmax_labels=argmax, dte=dte, trade_date=trade_date)
        diag = build_window_diagnostics([w], top_decile_frac=0.1)
        assert len(diag) == 1
        d = diag[0]
        assert d["argmax_call"]["n"] == n
        assert d["top_decile_of_argmax_call"]["n"] == 10  # top 10% of 100 argmax==CALL rows
        assert "date_concentration" in d["top_decile_of_argmax_call"]
        assert "near_expiry" in d["top_decile_of_argmax_call"]
        assert d["regime"]["vix_mean"] == pytest.approx(12.0)

    def test_concentrated_top_decile_flagged(self) -> None:
        n = 100
        proba = np.linspace(0, 1, n)
        argmax = np.array([CALL] * n, dtype=object)
        # Top decile (highest proba, last 10 rows) all share ONE trade_date.
        trade_date = np.array([f"2026-02-{i:02d}" for i in range(1, 91)] + ["2026-03-01"] * 10, dtype=object)
        w = _fake_window(1, n=n, proba_call=proba, net_ce=proba * 0.01, argmax_labels=argmax, trade_date=trade_date)
        diag = build_window_diagnostics([w], top_decile_frac=0.1)
        top = diag[0]["top_decile_of_argmax_call"]
        assert top["date_concentration"]["n_distinct_dates"] == 1
        assert top["date_concentration"]["top_date_share"] == pytest.approx(1.0)


def _snapshot_payload(*, minute: int, ce_ltp: float, pe_ltp: float, strike: int = 24500) -> dict:
    ts_ist_minute = 9 * 60 + 45 + minute
    hour, minute_of_hour = divmod(ts_ist_minute, 60)
    ts_ist = f"2026-01-05T{hour:02d}:{minute_of_hour:02d}:00"
    ts = pd.Timestamp(ts_ist) - pd.Timedelta(hours=5, minutes=30)
    return {
        "snapshot_id": f"s{minute}",
        "timestamp": ts.isoformat() + "+00:00",
        "trade_date": "2026-01-05",
        "session_context": {"date": "2026-01-05"},
        "chain_aggregates": {"atm_strike": strike, "pcr": 1.0},
        "atm_options": {"atm_ce_close": ce_ltp, "atm_pe_close": pe_ltp, "atm_ce_oi": 50000.0, "atm_pe_oi": 50000.0},
        "futures_bar": {"fut_close": 24500.0 + minute},
        "strikes": [{"strike": strike, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp}],
    }


def _write_day_parquet(parquet_base: Path, trade_date: str, payloads: list[dict]) -> None:
    day_dir = parquet_base / "snapshots" / f"trade_date={trade_date}"
    day_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"snapshot_raw_json": json.dumps(p, default=str)} for p in payloads]
    pd.DataFrame(rows).to_parquet(day_dir / "data.parquet", index=False)


@pytest.fixture
def synthetic_parquet_base(tmp_path: Path) -> Path:
    base = tmp_path / "parquet_data"
    payloads = [_snapshot_payload(minute=i, ce_ltp=100.0 + i * 3.0, pe_ltp=max(5.0, 100.0 - i * 1.0)) for i in range(40)]
    _write_day_parquet(base, "2026-01-05", payloads)
    return base


class TestRunMaskStressTest:
    def test_runs_end_to_end(self, synthetic_parquet_base: Path) -> None:
        n = 25
        trade_date = np.array(["2026-01-05"] * n, dtype=object)
        bar_index = np.arange(n)
        mask = np.zeros(n, dtype=bool)
        mask[::5] = True  # 5 fired bars
        result = run_mask_stress_test(
            trade_date, bar_index, mask, str(synthetic_parquet_base),
            horizon_bars=10, lot_size=65, leg="CE", min_paired_bars=1,
        )
        assert result["n_fired"] == 5
        assert result["leg"] == "CE"
        assert "ok" in result
        assert "mean_payoff_at_fire" in result

    def test_no_fired_bars(self, synthetic_parquet_base: Path) -> None:
        n = 10
        trade_date = np.array(["2026-01-05"] * n, dtype=object)
        bar_index = np.arange(n)
        mask = np.zeros(n, dtype=bool)
        result = run_mask_stress_test(
            trade_date, bar_index, mask, str(synthetic_parquet_base),
            horizon_bars=10, lot_size=65, min_paired_bars=1,
        )
        assert result["n_fired"] == 0


class TestEndToEndSmoke:
    """Needs optuna+xgboost -- skip (not fail) if unavailable."""

    def _make_synthetic_training_view(self, tmp_path: Path, *, n_days: int, bars_per_day: int) -> Path:
        from ml_pipeline_2.scripts.train_joint_direction_v1 import JOINT_FEATURES
        from ml_pipeline_2.pipeline.direction_margin import CALL_ONLY_FEATURES, PUT_ONLY_FEATURES

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
        csv_path = tmp_path / "training_view_smoke.csv.gz"
        df.to_csv(csv_path, index=False, compression="gzip")
        return csv_path

    def test_main_smoke(self, tmp_path: Path) -> None:
        pytest.importorskip("optuna")
        pytest.importorskip("xgboost")
        from ml_pipeline_2.scripts.study_joint_direction_call_threshold import main

        csv_path = self._make_synthetic_training_view(tmp_path, n_days=60, bars_per_day=30)
        output_json = tmp_path / "report.json"
        rc = main([
            "--training-view-csv", str(csv_path),
            "--n-windows", "2", "--min-train-days", "40",
            "--n-trials", "2", "--skip-stress-test",
            "--grid-n-boot", "50", "--grid-n-perm", "50",
            "--final-n-boot", "50", "--final-n-perm", "50",
            "--output-json", str(output_json),
        ])
        assert rc == 0
        assert output_json.exists()
        report = json.loads(output_json.read_text())
        assert "part1_no_filter_grid" in report
        assert "part2_entry_filtered_grid" in report
        assert "window_diagnostics" in report
        assert "headline_candidates" in report
        # No raw model objects leaked into the JSON.
        assert "XGBClassifier" not in json.dumps(report)
        assert "XGBRegressor" not in json.dumps(report)
