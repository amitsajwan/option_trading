"""Tests for train_joint_direction_v1.py. HPO/main() end-to-end tests need
optuna+xgboost (not installed in every environment this runs in -- skipped
gracefully via importorskip, matching test_train_direction_margin_v1.py's
own convention) -- the pure feature-list/verdict logic and the delayed-
stress-test glue (against synthetic parquet fixtures) are tested
unconditionally."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.direction_margin import CALL_ONLY_FEATURES, PUT_ONLY_FEATURES
from ml_pipeline_2.pipeline.joint_direction import CALL, NO_TRADE, PUT, CLASS_TO_INT
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3
from ml_pipeline_2.scripts.train_joint_direction_v1 import (
    JOINT_FEATURES,
    TRACK5_CALL_TOP_DECILE_BASELINE,
    TRACK5_PUT_TOP_DECILE_BASELINE,
    _select_features,
    class_verdict,
    run_class_stress_test,
)


class TestFeatureComposition:
    def test_joint_features_is_shared_plus_both_legs(self) -> None:
        assert set(ENTRY_FEATURES_V3).issubset(set(JOINT_FEATURES))
        for f in CALL_ONLY_FEATURES:
            assert f in JOINT_FEATURES
        for f in PUT_ONLY_FEATURES:
            assert f in JOINT_FEATURES

    def test_joint_features_has_no_duplicates(self) -> None:
        assert len(JOINT_FEATURES) == len(set(JOINT_FEATURES))

    def test_joint_features_sees_both_legs_unlike_track5_per_leg_models(self) -> None:
        # Track 5's CALL_FEATURES / PUT_FEATURES each only added ONE leg's
        # extras. The joint model must see BOTH -- it has to compare across
        # legs to make a 3-way NO_TRADE/CALL/PUT decision.
        only_call = set(CALL_ONLY_FEATURES) - set(ENTRY_FEATURES_V3)
        only_put = set(PUT_ONLY_FEATURES) - set(ENTRY_FEATURES_V3)
        assert only_call.issubset(set(JOINT_FEATURES))
        assert only_put.issubset(set(JOINT_FEATURES))


class TestSelectFeatures:
    def test_raises_on_missing_feature_columns(self) -> None:
        df = pd.DataFrame({"a": [1.0]})
        with pytest.raises(ValueError, match="missing"):
            _select_features(df, ["a", "b"])

    def test_coerces_to_numeric(self) -> None:
        df = pd.DataFrame({"a": ["1.5", "2.5"]})
        result = _select_features(df, ["a"])
        assert result["a"].dtype.kind == "f"


class TestTrack5BaselineConstants:
    def test_baselines_are_the_real_documented_negative_numbers(self) -> None:
        # Sanity-lock against silently drifting -- these are the exact,
        # real, pooled top_decile_mean numbers from track 5's own final
        # report (.run/direction_margin/nifty_direction_margin_FINAL.json),
        # both still net-negative.
        assert TRACK5_CALL_TOP_DECILE_BASELINE < 0
        assert TRACK5_PUT_TOP_DECILE_BASELINE < 0
        assert TRACK5_CALL_TOP_DECILE_BASELINE == pytest.approx(-0.00547)
        assert TRACK5_PUT_TOP_DECILE_BASELINE == pytest.approx(-0.00434)


class TestClassVerdict:
    def _call_eval(self, **overrides) -> dict:
        base = {"rows": 500, "top_decile_mean": 0.02, "bottom_decile_mean": -0.03, "spearman_rho": 0.15, "spearman_p_value": 0.01}
        base.update(overrides)
        return base

    def _no_trade_eval(self, **overrides) -> dict:
        base = {"rows": 500, "top_decile_mean": -0.02, "bottom_decile_mean": 0.03, "spearman_rho": -0.15, "spearman_p_value": 0.01}
        base.update(overrides)
        return base

    def test_call_clears_ship_gates_and_flags_stress_test(self) -> None:
        pooled = self._call_eval()
        per_window = [self._call_eval(spearman_rho=r) for r in (0.10, 0.20, 0.05, 0.12, 0.18)]
        v = class_verdict(pooled, per_window, class_name=CALL)
        assert v["ship_gates_all_pass"] is True
        assert v["run_stress_test"] is True
        assert v["frac_windows_consistent_sign"] == 1.0

    def test_call_fails_ship_gates_on_weak_rank_correlation(self) -> None:
        pooled = self._call_eval(spearman_rho=0.01)
        v = class_verdict(pooled, [pooled], class_name=CALL)
        assert v["ship_gates_all_pass"] is False
        assert v["run_stress_test"] is False

    def test_no_trade_clears_ship_gates_with_negative_rho(self) -> None:
        pooled = self._no_trade_eval()
        per_window = [self._no_trade_eval(spearman_rho=r) for r in (-0.10, -0.20, -0.05, -0.12, -0.18)]
        v = class_verdict(pooled, per_window, class_name=NO_TRADE)
        assert v["ship_gates_all_pass"] is True
        assert v["frac_windows_consistent_sign"] == 1.0

    def test_no_trade_never_runs_stress_test_even_when_gates_pass(self) -> None:
        pooled = self._no_trade_eval()
        v = class_verdict(pooled, [pooled], class_name=NO_TRADE)
        assert v["ship_gates_all_pass"] is True
        assert v["run_stress_test"] is False  # no leg to re-price

    def test_no_trade_fails_on_positive_rho(self) -> None:
        pooled = self._no_trade_eval(spearman_rho=0.10)
        v = class_verdict(pooled, [pooled], class_name=NO_TRADE)
        assert v["ship_gates_all_pass"] is False

    def test_reports_inconsistent_walk_forward_windows(self) -> None:
        pooled = self._call_eval()
        per_window = [self._call_eval(spearman_rho=r) for r in (0.30, -0.05, -0.10, 0.02, -0.01)]
        v = class_verdict(pooled, per_window, class_name=CALL)
        assert v["frac_windows_consistent_sign"] == pytest.approx(0.4)

    def test_handles_no_per_window_data(self) -> None:
        pooled = self._call_eval()
        v = class_verdict(pooled, [], class_name=CALL)
        assert v["frac_windows_consistent_sign"] == 0.0
        assert v["per_window_spearman_rho"] == []


# ── Delayed-action stress test glue (synthetic parquet, same format as
# test_train_direction_margin_v1.py) ────────────────────────────────────────

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
    # CE rallies steadily all day -- a "confidently CALL" call frozen early
    # should keep paying at later re-priced entries too (no repricing-wall
    # collapse in this synthetic fixture -- it exists to exercise the glue,
    # not to assert a real finding).
    payloads = [
        _snapshot_payload(minute=i, ce_ltp=100.0 + i * 3.0, pe_ltp=max(5.0, 100.0 - i * 1.0))
        for i in range(40)
    ]
    _write_day_parquet(base, "2026-01-05", payloads)
    return base


class TestRunClassStressTest:
    def test_runs_end_to_end_for_call(self, synthetic_parquet_base: Path) -> None:
        n = 25
        test_df = pd.DataFrame({"trade_date": ["2026-01-05"] * n, "bar_index": list(range(n))})
        proba = np.zeros((n, 3))
        proba[:, CLASS_TO_INT[CALL]] = np.linspace(0.0, 1.0, n)
        pred_labels = np.array([CALL] * n, dtype=object)
        fake_window = {"_test_df": test_df, "_proba": proba, "_pred_labels": pred_labels}
        result = run_class_stress_test(
            [fake_window], str(synthetic_parquet_base), target_class=CALL,
            horizon_bars=10, lot_size=65, top_decile_frac=0.2, min_paired_bars=1,
        )
        assert result["target_class"] == CALL
        assert result["leg"] == "CE"
        assert result["n_fired"] == 5  # top 20% of 25 argmax==CALL rows
        assert "ok" in result
        assert "mean_payoff_at_fire" in result

    def test_runs_end_to_end_for_put(self, synthetic_parquet_base: Path) -> None:
        n = 25
        test_df = pd.DataFrame({"trade_date": ["2026-01-05"] * n, "bar_index": list(range(n))})
        proba = np.zeros((n, 3))
        proba[:, CLASS_TO_INT[PUT]] = np.linspace(0.0, 1.0, n)
        pred_labels = np.array([PUT] * n, dtype=object)
        fake_window = {"_test_df": test_df, "_proba": proba, "_pred_labels": pred_labels}
        result = run_class_stress_test(
            [fake_window], str(synthetic_parquet_base), target_class=PUT,
            horizon_bars=10, lot_size=65, top_decile_frac=0.2, min_paired_bars=1,
        )
        assert result["target_class"] == PUT
        assert result["leg"] == "PE"
        assert result["n_fired"] == 5

    def test_ignores_bars_where_class_is_not_the_argmax(self, synthetic_parquet_base: Path) -> None:
        n = 20
        test_df = pd.DataFrame({"trade_date": ["2026-01-05"] * n, "bar_index": list(range(n))})
        proba = np.zeros((n, 3))
        # High P(CALL) everywhere, but only the first 5 rows are actually
        # argmax==CALL -- the rest are argmax==PUT despite a nonzero P(CALL).
        proba[:, CLASS_TO_INT[CALL]] = np.linspace(0.5, 1.0, n)
        pred_labels = np.array([CALL] * 5 + [PUT] * 15, dtype=object)
        fake_window = {"_test_df": test_df, "_proba": proba, "_pred_labels": pred_labels}
        result = run_class_stress_test(
            [fake_window], str(synthetic_parquet_base), target_class=CALL,
            horizon_bars=10, lot_size=65, top_decile_frac=0.5, min_paired_bars=1,
        )
        # top 50% of the 5 CALL-argmax rows -> k=max(1,round(5*0.5))=2 fired
        # (Python's banker's rounding: round(2.5)==2), never touching the
        # 15 PUT-argmax rows even though several have higher raw P(CALL).
        assert result["n_fired"] == 2

    def test_pools_fires_across_multiple_windows(self, synthetic_parquet_base: Path) -> None:
        def _mk_window(bar_offset: int) -> dict:
            n = 10
            test_df = pd.DataFrame({"trade_date": ["2026-01-05"] * n, "bar_index": list(range(bar_offset, bar_offset + n))})
            proba = np.zeros((n, 3))
            proba[:, CLASS_TO_INT[CALL]] = np.linspace(0.0, 1.0, n)
            pred_labels = np.array([CALL] * n, dtype=object)
            return {"_test_df": test_df, "_proba": proba, "_pred_labels": pred_labels}

        w1, w2 = _mk_window(0), _mk_window(10)
        result = run_class_stress_test(
            [w1, w2], str(synthetic_parquet_base), target_class=CALL,
            horizon_bars=10, lot_size=65, top_decile_frac=0.1, min_paired_bars=1,
        )
        assert result["n_fired"] == 2  # top 10% of pooled 20 rows

    def test_raises_on_no_trade_target(self, synthetic_parquet_base: Path) -> None:
        with pytest.raises(ValueError):
            run_class_stress_test([], str(synthetic_parquet_base), target_class=NO_TRADE, horizon_bars=10, lot_size=65)


class TestEndToEndHpoAndMain:
    """Needs optuna+xgboost -- skip (not fail) if unavailable. Purely a
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
                for f in JOINT_FEATURES:
                    if f not in row:
                        row[f] = rng.normal()
                # Weak real signal on each leg's own feature, matching the
                # sibling script's "something for HPO to find" convention.
                row["net_ce_return"] = row[CALL_ONLY_FEATURES[0]] * 0.01 + rng.normal(scale=0.02)
                row["net_pe_return"] = row[PUT_ONLY_FEATURES[0]] * 0.01 + rng.normal(scale=0.02)
                rows.append(row)
        df = pd.DataFrame(rows)
        csv_path = tmp_path / "training_view_smoke.csv.gz"
        df.to_csv(csv_path, index=False, compression="gzip")
        return csv_path

    def test_main_smoke(self, tmp_path: Path) -> None:
        pytest.importorskip("optuna")
        pytest.importorskip("xgboost")
        from ml_pipeline_2.scripts.train_joint_direction_v1 import main

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
        assert "overall_class_balance" in report
        # Headline: three independent per-class verdicts.
        assert "headline" in report
        for key in ("call_verdict", "put_verdict", "no_trade_verdict"):
            assert key in report["headline"]
        # --skip-stress-test was passed -- neither stress test should have run.
        assert report["headline"]["call_stress_test"] is None
        assert report["headline"]["put_stress_test"] is None
        # Critical comparison vs track 5 is always present.
        assert "call_vs_track5_independent_regressor" in report["headline"]
        assert "put_vs_track5_independent_regressor" in report["headline"]
        # No raw model/medians objects leaked into the JSON.
        for w in report["windows"]:
            if "model_meta" in w:
                assert "model" not in w["model_meta"]
                assert "medians" not in w["model_meta"]
            assert "XGBClassifier" not in json.dumps(w)
