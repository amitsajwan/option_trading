from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd

from strategy_app.contracts import SignalType
from strategy_app.engines.pure_ml_engine import PureMLEngine
from strategy_app.logging.signal_logger import SignalLogger


class _ConstantProbModel:
    def __init__(self, prob: float) -> None:
        self._prob = float(prob)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = int(len(x))
        p1 = np.full(shape=(n,), fill_value=self._prob, dtype=float)
        return np.column_stack([1.0 - p1, p1])


def _single_target_package(*, prob: float, model_key: str, prob_col: str, feature_columns: list[str]) -> dict[str, object]:
    return {
        "feature_columns": feature_columns,
        "_model_input_contract": {
            "required_features": feature_columns,
            "allow_extra_features": True,
            "missing_policy": "error",
            "contract_id": "snapshot_stage_view",
        },
        "single_target": {"model_key": model_key, "prob_col": prob_col},
        "models": {model_key: _ConstantProbModel(prob)},
    }


def _snapshot(ts: str) -> dict[str, object]:
    return {
        "snapshot_id": "snap-1",
        "trade_date": ts[:10],
        "timestamp": ts,
        "schema_name": "MarketSnapshot",
        "schema_version": "3.0",
        "session_context": {
            "snapshot_id": "snap-1",
            "timestamp": ts,
            "date": ts[:10],
            "session_phase": "ACTIVE",
            "days_to_expiry": 2,
            "is_expiry_day": False,
        },
        "futures_derived": {
            "fut_return_5m": 0.015,
            "fut_return_15m": 0.020,
            "realized_vol_30m": 0.010,
            "vol_ratio": 1.2,
            "price_vs_vwap": 0.001,
        },
        "chain_aggregates": {
            "atm_strike": 50000,
            "pcr": 1.10,
        },
        "vix_context": {
            "vix_current": 14.0,
        },
        "atm_options": {
            "atm_ce_close": 100.0,
            "atm_pe_close": 90.0,
            "atm_ce_oi": 150000.0,
            "atm_pe_oi": 120000.0,
            "atm_ce_volume": 30000.0,
            "atm_pe_volume": 25000.0,
        },
        "strikes": [
            {
                "strike": 50000.0,
                "ce_ltp": 100.0,
                "pe_ltp": 90.0,
                "ce_oi": 150000.0,
                "pe_oi": 120000.0,
                "ce_volume": 30000.0,
                "pe_volume": 25000.0,
            }
        ],
    }


class PureMLStagedEngineTests(unittest.TestCase):
    def _write_bundle(
        self,
        root: Path,
        *,
        recipe_probs: dict[str, float],
        runtime_gate_ids: list[str] | None = None,
        runtime_block_expiry: bool = False,
        stage1_feature_columns: list[str] | None = None,
        stage2_feature_columns: list[str] | None = None,
        stage3_feature_columns: list[str] | None = None,
    ) -> tuple[Path, Path]:
        model_path = root / "model.joblib"
        threshold_path = root / "thresholds.json"
        gate_ids = runtime_gate_ids or ["rollout_guard_v1", "feature_freshness_v1", "liquidity_gate_v1"]
        stage1_features = stage1_feature_columns or ["fut_return_5m", "pcr"]
        stage2_features = stage2_feature_columns or ["fut_return_5m", "pcr"]
        stage3_features = stage3_feature_columns or ["realized_vol_30m", "stage1_entry_prob", "stage2_direction_up_prob"]
        bundle = {
            "kind": "ml_pipeline_2_staged_runtime_bundle_v1",
            "runtime": {"prefilter_gate_ids": gate_ids, "block_expiry": runtime_block_expiry},
            "stages": {
                "stage1": {
                    "model_package": _single_target_package(prob=0.80, model_key="move", prob_col="move_prob", feature_columns=stage1_features),
                    "view_name": "stage1_entry_view",
                },
                "stage2": {
                    "model_package": _single_target_package(prob=0.75, model_key="direction", prob_col="direction_up_prob", feature_columns=stage2_features),
                    "view_name": "stage2_direction_view",
                },
                "stage3": {
                    "recipe_packages": {
                        recipe_id: _single_target_package(prob=prob, model_key="move", prob_col="move_prob", feature_columns=stage3_features)
                        for recipe_id, prob in recipe_probs.items()
                    },
                    "view_name": "stage3_recipe_view",
                },
            },
        }
        policy = {
            "kind": "ml_pipeline_2_staged_runtime_policy_v1",
            "stage1": {"selected_threshold": 0.55},
            "stage2": {"selected_ce_threshold": 0.60, "selected_pe_threshold": 0.60, "selected_min_edge": 0.10},
            "stage3": {"selected_threshold": 0.60, "selected_margin_min": 0.10},
            "runtime": {"prefilter_gate_ids": gate_ids, "block_expiry": runtime_block_expiry},
            "recipe_catalog": [
                {"recipe_id": "L0", "horizon_minutes": 15, "take_profit_pct": 0.0025, "stop_loss_pct": 0.0008},
                {"recipe_id": "L1", "horizon_minutes": 15, "take_profit_pct": 0.0020, "stop_loss_pct": 0.0008},
            ],
        }
        joblib.dump(bundle, model_path)
        threshold_path.write_text(json.dumps(policy), encoding="utf-8")
        return model_path, threshold_path

    def test_staged_bundle_emits_recipe_driven_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path, threshold_path = self._write_bundle(root, recipe_probs={"L0": 0.82, "L1": 0.55})
            engine = PureMLEngine(
                model_package_path=str(model_path),
                threshold_report_path=str(threshold_path),
                signal_logger=SignalLogger(root),
                max_feature_age_sec=10_000_000,
            )
            engine.on_session_start(date(2026, 3, 18))
            signal = engine.evaluate(_snapshot("2026-03-18T09:30:00+05:30"))
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            self.assertEqual(signal.direction, "CE")
            self.assertEqual(signal.entry_strategy_name, "ML_PURE_STAGED")
            self.assertEqual(signal.decision_mode, "ml_staged")
            self.assertEqual(signal.strategy_family_version, "ML_PURE_STAGED_V1")
            self.assertEqual(signal.strategy_profile_id, "ml_pure_staged_v1")
            self.assertEqual(signal.max_hold_bars, 15)
            self.assertAlmostEqual(signal.stop_loss_pct, 0.0008, places=6)
            self.assertAlmostEqual(signal.target_pct, 0.0025, places=6)

    def test_staged_bundle_holds_on_low_recipe_margin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path, threshold_path = self._write_bundle(root, recipe_probs={"L0": 0.71, "L1": 0.66})
            engine = PureMLEngine(
                model_package_path=str(model_path),
                threshold_report_path=str(threshold_path),
                signal_logger=SignalLogger(root),
                max_feature_age_sec=10_000_000,
            )
            engine.on_session_start(date(2026, 3, 18))
            signal = engine.evaluate(_snapshot("2026-03-18T09:30:00+05:30"))
            self.assertIsNone(signal)

    def test_staged_bundle_bypass_gates_allows_entry_on_low_recipe_margin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path, threshold_path = self._write_bundle(root, recipe_probs={"L0": 0.71, "L1": 0.66})
            with patch.dict(os.environ, {"STRATEGY_ML_PURE_BYPASS_GATES": "1"}, clear=False):
                engine = PureMLEngine(
                    model_package_path=str(model_path),
                    threshold_report_path=str(threshold_path),
                    signal_logger=SignalLogger(root),
                    max_feature_age_sec=10_000_000,
                )
                engine.on_session_start(date(2026, 3, 18))
                signal = engine.evaluate(_snapshot("2026-03-18T09:30:00+05:30"))
                self.assertIsNotNone(signal)
                assert signal is not None
                self.assertEqual(signal.signal_type, SignalType.ENTRY)

    def test_staged_bundle_requires_explicit_policy_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path, threshold_path = self._write_bundle(root, recipe_probs={"L0": 0.82, "L1": 0.55})
            payload = json.loads(threshold_path.read_text(encoding="utf-8"))
            del payload["stage2"]["selected_ce_threshold"]
            threshold_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "staged runtime policy missing stage2.selected_ce_threshold"):
                PureMLEngine(
                    model_package_path=str(model_path),
                    threshold_report_path=str(threshold_path),
                    signal_logger=SignalLogger(root),
                    max_feature_age_sec=10_000_000,
                )

    def test_staged_bundle_backfills_missing_stage_view_fields_from_rolling_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path, threshold_path = self._write_bundle(
                root,
                recipe_probs={"L0": 0.82, "L1": 0.55},
                runtime_gate_ids=["rollout_guard_v1", "feature_completeness_v1", "feature_freshness_v1", "liquidity_gate_v1"],
                stage1_feature_columns=["fut_return_5m"],
                stage2_feature_columns=["fut_return_5m"],
                stage3_feature_columns=["stage1_entry_prob", "stage2_direction_up_prob"],
            )
            engine = PureMLEngine(
                model_package_path=str(model_path),
                threshold_report_path=str(threshold_path),
                signal_logger=SignalLogger(root),
                max_feature_age_sec=10_000_000,
                max_nan_features=0,
            )
            engine.on_session_start(date(2026, 3, 18))

            signal = None

    def test_staged_bundle_backfills_snapshot_year_for_stage3(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path, threshold_path = self._write_bundle(
                root,
                recipe_probs={"L0": 0.82, "L1": 0.55},
                runtime_gate_ids=["rollout_guard_v1", "feature_freshness_v1", "liquidity_gate_v1"],
                stage3_feature_columns=["year", "stage1_entry_prob", "stage2_direction_up_prob"],
            )
            engine = PureMLEngine(
                model_package_path=str(model_path),
                threshold_report_path=str(threshold_path),
                signal_logger=SignalLogger(root),
                max_feature_age_sec=10_000_000,
            )
            engine.on_session_start(date(2026, 3, 18))
            signal = engine.evaluate(_snapshot("2026-03-18T09:30:00+05:30"))
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            assert signal is not None
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            self.assertEqual(signal.direction, "CE")

    def test_staged_bundle_warns_when_constructor_min_edge_is_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path, threshold_path = self._write_bundle(root, recipe_probs={"L0": 0.82, "L1": 0.55})
            with self.assertLogs("strategy_app.engines.pure_ml_engine", level="WARNING") as logs:
                PureMLEngine(
                    model_package_path=str(model_path),
                    threshold_report_path=str(threshold_path),
                    signal_logger=SignalLogger(root),
                    max_feature_age_sec=10_000_000,
                    min_edge=0.05,
                )
            self.assertTrue(any("ignores constructor min_edge" in message for message in logs.output))

    def test_staged_bundle_can_block_expiry_via_runtime_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path, threshold_path = self._write_bundle(
                root,
                recipe_probs={"L0": 0.82, "L1": 0.55},
                runtime_gate_ids=["rollout_guard_v1", "regime_gate_v1", "feature_freshness_v1", "liquidity_gate_v1"],
                runtime_block_expiry=True,
            )
            engine = PureMLEngine(
                model_package_path=str(model_path),
                threshold_report_path=str(threshold_path),
                signal_logger=SignalLogger(root),
                max_feature_age_sec=10_000_000,
            )
            engine.on_session_start(date(2026, 3, 18))
            snap = _snapshot("2026-03-18T09:30:00+05:30")
            snap["session_context"]["is_expiry_day"] = True
            snap["session_context"]["days_to_expiry"] = 0

            signal = engine.evaluate(snap)

            self.assertIsNone(signal)

    def test_staged_bundle_default_runtime_policy_does_not_block_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            model_path, threshold_path = self._write_bundle(
                root,
                recipe_probs={"L0": 0.82, "L1": 0.55},
                runtime_gate_ids=["rollout_guard_v1", "regime_gate_v1", "feature_freshness_v1", "liquidity_gate_v1"],
                runtime_block_expiry=False,
            )
            engine = PureMLEngine(
                model_package_path=str(model_path),
                threshold_report_path=str(threshold_path),
                signal_logger=SignalLogger(root),
                max_feature_age_sec=10_000_000,
            )
            engine.on_session_start(date(2026, 3, 18))
            snap = _snapshot("2026-03-18T09:30:00+05:30")
            snap["session_context"]["is_expiry_day"] = True
            snap["session_context"]["days_to_expiry"] = 0

            signal = engine.evaluate(snap)

            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            self.assertEqual(signal.direction, "CE")


if __name__ == "__main__":
    unittest.main()
