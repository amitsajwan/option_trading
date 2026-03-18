from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

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
    def _write_bundle(self, root: Path, *, recipe_probs: dict[str, float]) -> tuple[Path, Path]:
        model_path = root / "model.joblib"
        threshold_path = root / "thresholds.json"
        bundle = {
            "kind": "ml_pipeline_2_staged_runtime_bundle_v1",
            "runtime": {"prefilter_gate_ids": ["rollout_guard_v1", "feature_freshness_v1", "liquidity_gate_v1"]},
            "stages": {
                "stage1": {
                    "model_package": _single_target_package(prob=0.80, model_key="move", prob_col="move_prob", feature_columns=["fut_return_5m", "pcr"]),
                    "view_name": "stage1_entry_view",
                },
                "stage2": {
                    "model_package": _single_target_package(prob=0.75, model_key="direction", prob_col="direction_up_prob", feature_columns=["fut_return_5m", "pcr"]),
                    "view_name": "stage2_direction_view",
                },
                "stage3": {
                    "recipe_packages": {
                        recipe_id: _single_target_package(prob=prob, model_key="move", prob_col="move_prob", feature_columns=["realized_vol_30m", "stage1_entry_prob", "stage2_direction_up_prob"])
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
            "runtime": {"prefilter_gate_ids": ["rollout_guard_v1", "feature_freshness_v1", "liquidity_gate_v1"]},
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


if __name__ == "__main__":
    unittest.main()
