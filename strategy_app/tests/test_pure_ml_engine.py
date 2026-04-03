import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import joblib

from strategy_app.contracts import SignalType
from strategy_app.engines.pure_ml_engine import PureMLEngine
from strategy_app.engines.runtime_artifacts import RuntimeArtifactStore
from strategy_app.engines.pure_ml_staged_runtime import StagedRuntimeDecision
from strategy_app.logging.signal_logger import SignalLogger


def _snapshot(
    *,
    snapshot_id: str,
    ts: str,
    ce_ltp: float = 100.0,
    pe_ltp: float = 100.0,
    ce_oi: float = 120000.0,
    pe_oi: float = 120000.0,
    ce_volume: float = 20000.0,
    pe_volume: float = 20000.0,
    is_expiry_day: bool = False,
    days_to_expiry: int = 2,
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "session_context": {
            "snapshot_id": snapshot_id,
            "timestamp": ts,
            "date": ts[:10],
            "session_phase": "ACTIVE",
            "days_to_expiry": days_to_expiry,
            "is_expiry_day": is_expiry_day,
            "minutes_since_open": 15,
            "day_of_week": 0,
        },
        "futures_derived": {
            "fut_return_5m": 0.01,
            "fut_return_15m": 0.015,
            "fut_return_30m": 0.02,
            "realized_vol_30m": 0.01,
            "vol_ratio": 1.4,
            "fut_oi_change_30m": 1500.0,
            "fut_oi": 100000.0,
            "fut_volume_ratio": 1.3,
            "price_vs_vwap": 0.001,
            "ema_9": 50010.0,
            "ema_21": 50000.0,
        },
        "opening_range": {
            "orh_broken": True,
            "orl_broken": False,
            "or_width": 100.0,
            "price_vs_orh": 0.005,
            "price_vs_orl": 0.015,
        },
        "vix_context": {
            "vix_current": 15.0,
            "vix_prev_close": 14.5,
            "vix_intraday_chg": 2.0,
            "vix_spike_flag": False,
        },
        "chain_aggregates": {
            "atm_strike": 50000,
            "total_ce_oi": 1_000_000.0,
            "total_pe_oi": 900_000.0,
            "pcr": 1.25,
        },
        "atm_options": {
            "atm_ce_close": ce_ltp,
            "atm_pe_close": pe_ltp,
            "atm_ce_volume": ce_volume,
            "atm_pe_volume": pe_volume,
            "atm_ce_oi": ce_oi,
            "atm_pe_oi": pe_oi,
            "atm_ce_iv": 0.16,
            "atm_pe_iv": 0.17,
            "atm_ce_vol_ratio": 1.1,
            "atm_pe_vol_ratio": 1.0,
            "atm_ce_oi_change_30m": 8000.0,
            "atm_pe_oi_change_30m": 4000.0,
        },
        "iv_derived": {"iv_skew": -0.01},
        "strikes": [
            {
                "strike": 50000.0,
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_volume": ce_volume,
                "pe_volume": pe_volume,
            }
        ],
    }


class PureMLEngineTests(unittest.TestCase):
    def _write_model_bundle(self, root: Path) -> Path:
        path = root / "model.joblib"
        bundle = {
            "kind": "ml_pipeline_2_staged_runtime_bundle_v1",
            "runtime": {
                "prefilter_gate_ids": ["valid_entry_phase_v1"],
                "block_expiry": False,
            },
            "stages": {
                "stage1": {"model_package": {"feature_columns": ["ret_5m"]}},
                "stage2": {"model_package": {"feature_columns": ["ret_5m"]}},
                "stage3": {"recipe_packages": {"base": {"feature_columns": ["ret_5m"]}}},
            },
        }
        joblib.dump(bundle, path)
        return path

    def _write_threshold_report(self, root: Path) -> Path:
        path = root / "thresholds.json"
        payload = {
            "kind": "ml_pipeline_2_staged_runtime_policy_v1",
            "stage1": {"selected_threshold": 0.60},
            "stage2": {
                "selected_ce_threshold": 0.60,
                "selected_pe_threshold": 0.60,
                "selected_min_edge": 0.15,
            },
            "stage3": {
                "selected_threshold": 0.55,
                "selected_margin_min": 0.05,
            },
            "runtime": {
                "prefilter_gate_ids": ["valid_entry_phase_v1"],
                "block_expiry": False,
            },
            "recipe_catalog": [
                {
                    "recipe_id": "base",
                    "horizon_minutes": 15,
                    "take_profit_pct": 0.20,
                    "stop_loss_pct": 0.05,
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _build_engine(self, root: Path, *, runtime_artifact_dir: Path | None = None) -> PureMLEngine:
        return PureMLEngine(
            model_package_path=str(self._write_model_bundle(root)),
            threshold_report_path=str(self._write_threshold_report(root)),
            signal_logger=SignalLogger(root),
            runtime_artifact_dir=runtime_artifact_dir,
        )

    def test_staged_buy_ce_emits_entry_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root)
            engine.on_session_start(date(2026, 3, 2))
            staged = StagedRuntimeDecision(
                action="BUY_CE",
                reason="recipe_selected",
                entry_prob=0.84,
                direction_up_prob=0.79,
                ce_prob=0.79,
                pe_prob=0.21,
                recipe_id="base",
                recipe_prob=0.92,
                recipe_margin=0.30,
                horizon_minutes=12,
                stop_loss_pct=0.04,
                target_pct=0.18,
            )

            with patch("strategy_app.engines.pure_ml_engine.predict_staged", return_value=staged):
                signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            self.assertEqual(signal.direction, "CE")
            self.assertEqual(signal.source, "ML_PURE")
            self.assertEqual(signal.entry_strategy_name, "ML_PURE_STAGED")
            self.assertEqual(signal.max_hold_bars, 12)
            self.assertAlmostEqual(float(signal.stop_loss_pct or 0.0), 0.04, places=6)
            self.assertAlmostEqual(float(signal.target_pct or 0.0), 0.18, places=6)
            self.assertAlmostEqual(float(signal.confidence or 0.0), 0.79, places=6)

    def test_staged_buy_pe_emits_entry_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root)
            engine.on_session_start(date(2026, 3, 2))
            staged = StagedRuntimeDecision(
                action="BUY_PE",
                reason="recipe_selected",
                entry_prob=0.82,
                direction_up_prob=0.18,
                ce_prob=0.18,
                pe_prob=0.82,
                recipe_id="base",
                recipe_prob=0.88,
                recipe_margin=0.22,
                horizon_minutes=10,
                stop_loss_pct=0.05,
                target_pct=0.16,
            )

            with patch("strategy_app.engines.pure_ml_engine.predict_staged", return_value=staged):
                signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:31:00+05:30"))

            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            self.assertEqual(signal.direction, "PE")
            self.assertEqual(signal.entry_strategy_name, "ML_PURE_STAGED")
            self.assertEqual(signal.max_hold_bars, 10)
            self.assertAlmostEqual(float(signal.confidence or 0.0), 0.82, places=6)

    def test_staged_hold_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root)
            engine.on_session_start(date(2026, 3, 2))
            staged = StagedRuntimeDecision(action="HOLD", reason="entry_below_threshold")

            with patch("strategy_app.engines.pure_ml_engine.predict_staged", return_value=staged):
                signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:32:00+05:30"))

            self.assertIsNone(signal)

    def test_runtime_artifacts_capture_session_state_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifacts_dir = root / "artifacts"
            engine = self._build_engine(root, runtime_artifact_dir=artifacts_dir)
            engine.set_run_context("runtime-test", {"strategy_profile_id": "ml_pure_staged_v1"})
            engine.on_session_start(date(2026, 3, 2))

            hold = StagedRuntimeDecision(action="HOLD", reason="entry_below_threshold", entry_prob=0.41, direction_up_prob=0.39, ce_prob=0.38, pe_prob=0.37, recipe_id="base", recipe_prob=0.52, recipe_margin=0.10)
            entry = StagedRuntimeDecision(action="BUY_CE", reason="recipe_selected", entry_prob=0.84, direction_up_prob=0.79, ce_prob=0.79, pe_prob=0.21, recipe_id="base", recipe_prob=0.92, recipe_margin=0.30, horizon_minutes=12, stop_loss_pct=0.04, target_pct=0.18)

            with patch("strategy_app.engines.pure_ml_engine.predict_staged", return_value=hold):
                self.assertIsNone(engine.evaluate(_snapshot(snapshot_id="snap-hold", ts="2026-03-02T09:30:00+05:30")))
            with patch("strategy_app.engines.pure_ml_engine.predict_staged", return_value=entry):
                signal = engine.evaluate(_snapshot(snapshot_id="snap-entry", ts="2026-03-02T09:31:00+05:30"))

            self.assertIsNotNone(signal)
            store = RuntimeArtifactStore(artifacts_dir)
            state = store.read_state()
            metrics = store.read_metrics(tail_lines=5)

            self.assertTrue(state["exists"])
            self.assertTrue(metrics["exists"])
            self.assertEqual(state["payload"]["session"]["bars_evaluated"], 2)
            self.assertEqual(state["payload"]["session"]["entries_taken"], 1)
            self.assertEqual(state["payload"]["session"]["hold_counts"]["entry_below_threshold"], 1)
            self.assertTrue(state["payload"]["position"]["has_position"])
            self.assertGreaterEqual(metrics["line_count"], 3)
            self.assertEqual(metrics["latest"]["event"], "entry")
            trace_lines = (root / "decision_traces.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertGreaterEqual(len(trace_lines), 2)
            latest_trace = json.loads(trace_lines[-1])
            self.assertEqual(latest_trace["final_outcome"], "entry_taken")
            self.assertEqual(latest_trace["engine_mode"], "ml_pure")

    def test_session_start_resets_runtime_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifacts_dir = root / "artifacts"
            engine = self._build_engine(root, runtime_artifact_dir=artifacts_dir)
            engine.set_run_context("runtime-test", {"strategy_profile_id": "ml_pure_staged_v1"})
            engine.on_session_start(date(2026, 3, 2))

            hold = StagedRuntimeDecision(action="HOLD", reason="feature_stale")
            with patch("strategy_app.engines.pure_ml_engine.predict_staged", return_value=hold):
                self.assertIsNone(engine.evaluate(_snapshot(snapshot_id="snap-hold", ts="2026-03-02T09:30:00+05:30")))

            engine.on_session_start(date(2026, 3, 3))

            state = RuntimeArtifactStore(artifacts_dir).read_state()
            self.assertTrue(state["exists"])
            self.assertEqual(state["payload"]["session"]["trade_date"], "2026-03-03")
            self.assertEqual(state["payload"]["session"]["bars_evaluated"], 0)
            self.assertEqual(state["payload"]["session"]["entries_taken"], 0)
            self.assertEqual(state["payload"]["session"]["hold_counts"], {})
            self.assertIsNone(state["payload"]["session"]["last_entry_at"])


if __name__ == "__main__":
    unittest.main()
