"""Tests for train_direction_margin_v1.py. HPO/main() end-to-end tests need
optuna+xgboost (not installed in every environment this runs in -- skipped
gracefully, not silently, via importorskip, matching
test_train_entry_option_payoff_v1.py's convention) -- the pure feature-list/
eval-helper logic and the delayed-stress-test glue (against synthetic
parquet fixtures) are tested unconditionally."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.direction_margin import CALL_ONLY_FEATURES, PUT_ONLY_FEATURES
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3
from ml_pipeline_2.scripts.train_direction_margin_v1 import (
    CALL_FEATURES,
    PUT_FEATURES,
    _regression_eval,
    _select_features,
    leg_verdict,
    run_delayed_stress_test,
    run_leg_stress_test,
)


class TestFeatureComposition:
    def test_call_features_is_shared_plus_call_only(self) -> None:
        assert set(ENTRY_FEATURES_V3).issubset(set(CALL_FEATURES))
        for f in CALL_ONLY_FEATURES:
            assert f in CALL_FEATURES

    def test_put_features_is_shared_plus_put_only(self) -> None:
        assert set(ENTRY_FEATURES_V3).issubset(set(PUT_FEATURES))
        for f in PUT_ONLY_FEATURES:
            assert f in PUT_FEATURES

    def test_call_and_put_features_differ_only_in_their_own_leg_additions(self) -> None:
        only_in_call = set(CALL_FEATURES) - set(PUT_FEATURES)
        only_in_put = set(PUT_FEATURES) - set(CALL_FEATURES)
        assert only_in_call == set(CALL_ONLY_FEATURES)
        assert only_in_put == set(PUT_ONLY_FEATURES)


class TestSelectFeatures:
    def test_raises_on_missing_feature_columns(self) -> None:
        df = pd.DataFrame({"a": [1.0]})
        with pytest.raises(ValueError, match="missing"):
            _select_features(df, ["a", "b"])

    def test_coerces_to_numeric(self) -> None:
        df = pd.DataFrame({"a": ["1.5", "2.5"]})
        result = _select_features(df, ["a"])
        assert result["a"].dtype.kind == "f"


class TestRegressionEval:
    def test_perfect_rank_correlation(self) -> None:
        predicted = np.arange(100, dtype=float)
        actual = np.arange(100, dtype=float) * 0.01
        result = _regression_eval(predicted, actual)
        assert result["spearman_rho"] == pytest.approx(1.0, abs=1e-6)
        assert result["rows"] == 100
        assert len(result["decile_table"]) == 10
        assert result["top_decile_mean"] > result["bottom_decile_mean"]

    def test_nan_actual_rows_are_excluded(self) -> None:
        predicted = np.array([1.0, 2.0, 3.0, 4.0])
        actual = np.array([0.1, np.nan, 0.3, np.nan])
        result = _regression_eval(predicted, actual)
        assert result["rows"] == 2

    def test_too_few_rows_returns_none_stats_not_a_crash(self) -> None:
        result = _regression_eval(np.array([1.0]), np.array([0.1]))
        assert result["rows"] == 1
        assert result["spearman_rho"] is None
        assert result["decile_table"] == []


# ── Delayed-action stress test glue (synthetic parquet, same format as
# test_build_direction_margin_training_view.py) ────────────────────────────

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
        "atm_options": {
            "atm_ce_close": ce_ltp, "atm_pe_close": pe_ltp,
            "atm_ce_oi": 50000.0, "atm_pe_oi": 50000.0,
        },
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
    # CE rallies steadily all day -- a "confidently CE" call frozen early
    # should keep paying at later re-priced entries too (no repricing-wall
    # collapse in this synthetic fixture -- it exists to exercise the glue,
    # not to assert a real finding).
    payloads = [
        _snapshot_payload(minute=i, ce_ltp=100.0 + i * 3.0, pe_ltp=max(5.0, 100.0 - i * 1.0))
        for i in range(40)
    ]
    _write_day_parquet(base, "2026-01-05", payloads)
    return base


class TestLegVerdict:
    def _eval(self, **overrides) -> dict:
        base = {"rows": 500, "top_decile_mean": 0.02, "spearman_rho": 0.15, "spearman_p_value": 0.01}
        base.update(overrides)
        return base

    def test_clears_ship_gates_and_flags_stress_test(self) -> None:
        pooled = self._eval()
        per_window = [self._eval(spearman_rho=r) for r in (0.10, 0.20, 0.05, 0.12, 0.18)]
        v = leg_verdict(pooled, per_window)
        assert v["ship_gates_all_pass"] is True
        assert v["run_stress_test"] is True
        assert v["frac_windows_with_positive_rho"] == 1.0

    def test_fails_ship_gates_on_weak_rank_correlation(self) -> None:
        pooled = self._eval(spearman_rho=0.01)
        v = leg_verdict(pooled, [pooled])
        assert v["ship_gates_all_pass"] is False
        assert v["run_stress_test"] is False

    def test_fails_ship_gates_when_top_decile_not_profitable(self) -> None:
        pooled = self._eval(top_decile_mean=-0.01)
        v = leg_verdict(pooled, [pooled])
        assert not v["ship_gates"]["top_decile_mean_payoff>0.0"]
        assert v["ship_gates_all_pass"] is False

    def test_reports_inconsistent_walk_forward_windows(self) -> None:
        pooled = self._eval()
        per_window = [self._eval(spearman_rho=r) for r in (0.30, -0.05, -0.10, 0.02, -0.01)]
        v = leg_verdict(pooled, per_window)
        assert v["frac_windows_with_positive_rho"] == pytest.approx(0.4)

    def test_handles_no_per_window_data(self) -> None:
        pooled = self._eval()
        v = leg_verdict(pooled, [])
        assert v["frac_windows_with_positive_rho"] == 0.0
        assert v["per_window_spearman_rho"] == []


class TestRunLegStressTest:
    def test_runs_end_to_end_for_call_leg(self, synthetic_parquet_base: Path) -> None:
        n = 25
        test_df = pd.DataFrame({"trade_date": ["2026-01-05"] * n, "bar_index": list(range(n))})
        call_pred = np.linspace(0.0, 1.0, n)  # monotonically increasing -- top decile = last few rows
        fake_window = {"_test_df": test_df, "_call_pred": call_pred}
        result = run_leg_stress_test(
            [fake_window], str(synthetic_parquet_base), leg="CE", pred_key="_call_pred",
            horizon_bars=10, lot_size=65, top_decile_frac=0.2, min_paired_bars=1,
        )
        assert result["leg"] == "CE"
        assert result["n_fired"] == 5  # top 20% of 25 rows
        assert "ok" in result
        assert "mean_payoff_at_fire" in result

    def test_runs_end_to_end_for_put_leg(self, synthetic_parquet_base: Path) -> None:
        n = 25
        test_df = pd.DataFrame({"trade_date": ["2026-01-05"] * n, "bar_index": list(range(n))})
        put_pred = np.linspace(0.0, 1.0, n)
        fake_window = {"_test_df": test_df, "_put_pred": put_pred}
        result = run_leg_stress_test(
            [fake_window], str(synthetic_parquet_base), leg="PE", pred_key="_put_pred",
            horizon_bars=10, lot_size=65, top_decile_frac=0.2, min_paired_bars=1,
        )
        assert result["leg"] == "PE"
        assert result["n_fired"] == 5

    def test_pools_fires_across_multiple_windows(self, synthetic_parquet_base: Path) -> None:
        w1 = {
            "_test_df": pd.DataFrame({"trade_date": ["2026-01-05"] * 10, "bar_index": list(range(10))}),
            "_call_pred": np.linspace(0.0, 1.0, 10),
        }
        w2 = {
            "_test_df": pd.DataFrame({"trade_date": ["2026-01-05"] * 10, "bar_index": list(range(10, 20))}),
            "_call_pred": np.linspace(0.0, 1.0, 10),
        }
        result = run_leg_stress_test(
            [w1, w2], str(synthetic_parquet_base), leg="CE", pred_key="_call_pred",
            horizon_bars=10, lot_size=65, top_decile_frac=0.1, min_paired_bars=1,
        )
        assert result["n_fired"] == 2  # top 10% of pooled 20 rows


class TestRunDelayedStressTest:
    def test_runs_end_to_end_on_a_single_fake_window(self, synthetic_parquet_base: Path) -> None:
        # bar_index 0..24 are all comfortably before the horizon/delay tail
        # (40 bars total, horizon_bars=10, delay up to 2 -> needs
        # bar_index + 10 + 2 < 40).
        n = 25
        test_df = pd.DataFrame({"trade_date": ["2026-01-05"] * n, "bar_index": list(range(n))})
        # Strongly CE-favoring margin for every row -- puts every row in the
        # "confident" subset regardless of confident_frac.
        predicted_margin = np.full(n, 5.0)
        fake_window = {"_test_df": test_df, "_predicted_margin": predicted_margin}

        result = run_delayed_stress_test(
            [fake_window], str(synthetic_parquet_base),
            horizon_bars=10, lot_size=65, confident_frac=1.0, edge_threshold=0.55,
        )
        assert "direction_flip_test" in result
        assert "called_leg_payoff_test" in result
        assert result["n_confident_bars"] == n
        # CE rallies the whole day in this fixture -- calling CE should be a
        # real, non-collapsing edge (ok=True either way: either a genuine
        # surviving edge, or "no edge at fire-time" -- both are ok=True).
        assert result["direction_flip_test"]["ok"] is True
        assert result["called_leg_payoff_test"]["ok"] is True

    def test_confident_frac_selects_a_subset(self, synthetic_parquet_base: Path) -> None:
        n = 25
        test_df = pd.DataFrame({"trade_date": ["2026-01-05"] * n, "bar_index": list(range(n))})
        rng = np.random.default_rng(0)
        predicted_margin = rng.normal(size=n)
        fake_window = {"_test_df": test_df, "_predicted_margin": predicted_margin}
        result = run_delayed_stress_test(
            [fake_window], str(synthetic_parquet_base),
            horizon_bars=10, lot_size=65, confident_frac=0.1, edge_threshold=0.55, min_paired_bars=1,
        )
        # frac=0.1 of 25 -> max(1, round(2.5))=2 per tail -> 4 confident bars
        assert result["n_confident_bars"] == 4


class TestEndToEndHpoAndMain:
    """Needs optuna+xgboost -- skip (not fail) if unavailable, matching
    train_entry_option_payoff_v1's own end-to-end test convention. Purely a
    wiring smoke test on tiny synthetic data -- window status may
    legitimately come back 'degenerate' if a window's inner-train slice is
    too thin, this only confirms the pipeline runs end-to-end without
    crashing and produces the expected report shape."""

    def _make_synthetic_training_view(self, tmp_path: Path, *, n_days: int, bars_per_day: int) -> Path:
        rng = np.random.default_rng(11)
        dates = pd.date_range("2026-01-01", periods=n_days, freq="D").strftime("%Y-%m-%d")
        rows = []
        for d in dates:
            for b in range(bars_per_day):
                row = {"trade_date": d, "bar_index": b}
                for f in CALL_FEATURES + PUT_FEATURES:
                    if f not in row:
                        row[f] = rng.normal()
                # Weak real signal on each leg's own feature, matching the
                # sibling script's "something for HPO to find" convention.
                row["net_ce_return"] = row[CALL_ONLY_FEATURES[0]] * 0.01 + rng.normal(scale=0.02)
                row["net_pe_return"] = row[PUT_ONLY_FEATURES[0]] * 0.01 + rng.normal(scale=0.02)
                row["payoff_winning_side"] = "CE" if row["net_ce_return"] >= row["net_pe_return"] else "PE"
                rows.append(row)
        df = pd.DataFrame(rows)
        csv_path = tmp_path / "training_view_smoke.csv.gz"
        df.to_csv(csv_path, index=False, compression="gzip")
        return csv_path

    def test_main_smoke(self, tmp_path: Path) -> None:
        pytest.importorskip("optuna")
        pytest.importorskip("xgboost")
        from ml_pipeline_2.scripts.train_direction_margin_v1 import main

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
        assert "windows" in report
        assert "pooled" in report
        assert len(report["windows"]) == 2
        # Headline: two independent per-leg verdicts, never combined.
        assert "headline" in report
        assert "call_leg_verdict" in report["headline"]
        assert "put_leg_verdict" in report["headline"]
        assert "ship_gates_all_pass" in report["headline"]["call_leg_verdict"]
        assert "ship_gates_all_pass" in report["headline"]["put_leg_verdict"]
        # --skip-stress-test was passed -- neither leg's stress test should have run.
        assert report["headline"]["call_leg_stress_test"] is None
        assert report["headline"]["put_leg_stress_test"] is None
        # Secondary margin section still present, clearly separated.
        assert "secondary_margin_section" in report
        assert "promising_verdict" in report["secondary_margin_section"]
        # No raw model/medians objects leaked into the JSON -- call_model/
        # put_model sub-dicts should carry only their plain-data fields.
        for w in report["windows"]:
            for key in ("call_model", "put_model"):
                if key in w:
                    assert "model" not in w[key]
                    assert "medians" not in w[key]
            assert "XGBRegressor" not in json.dumps(w)
