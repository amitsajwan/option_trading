import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib

from strategy_app.engines.pure_ml_engine import PureMLEngine
from strategy_app.logging.signal_logger import SignalLogger
from strategy_app.main import (
    _resolve_ml_pure_float,
    _resolve_ml_pure_int,
    _resolve_ml_pure_model_group,
    _resolve_ml_pure_model_package,
    _resolve_ml_pure_run_id,
    _resolve_ml_pure_switch_paths,
    _resolve_ml_pure_threshold_report,
    build_engine,
)


class PureMLConfigTests(unittest.TestCase):
    def _write_model_bundle(self, root: Path) -> Path:
        path = root / "model.joblib"
        bundle = {
            "kind": "ml_pipeline_2_staged_runtime_bundle_v1",
            "runtime": {
                "prefilter_gate_ids": ["valid_entry_phase_v1"],
                "block_expiry": True,
            },
            "stages": {
                "stage1": {
                    "model_package": {
                        "feature_columns": ["ret_5m"],
                    }
                },
                "stage2": {
                    "model_package": {
                        "feature_columns": ["ret_5m"],
                    }
                },
                "stage3": {
                    "recipe_packages": {
                        "base": {
                            "feature_columns": ["ret_5m"],
                        }
                    }
                },
            },
        }
        joblib.dump(bundle, path)
        return path

    def _write_threshold_report(self, root: Path) -> Path:
        path = root / "thresholds.json"
        payload = {
            "kind": "ml_pipeline_2_staged_runtime_policy_v1",
            "stage1": {
                "selected_threshold": 0.60,
            },
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
                "block_expiry": True,
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

    def test_resolve_ml_pure_paths_prefers_cli(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ML_PURE_MODEL_PACKAGE": "env-model.joblib",
                "ML_PURE_THRESHOLD_REPORT": "env-thresholds.json",
            },
            clear=False,
        ):
            self.assertEqual(_resolve_ml_pure_model_package("cli-model.joblib"), "cli-model.joblib")
            self.assertEqual(_resolve_ml_pure_threshold_report("cli-thresholds.json"), "cli-thresholds.json")

    def test_resolve_ml_pure_paths_uses_env_when_cli_missing(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ML_PURE_MODEL_PACKAGE": "env-model.joblib",
                "ML_PURE_THRESHOLD_REPORT": "env-thresholds.json",
            },
            clear=False,
        ):
            self.assertEqual(_resolve_ml_pure_model_package(None), "env-model.joblib")
            self.assertEqual(_resolve_ml_pure_threshold_report(None), "env-thresholds.json")

    def test_resolve_ml_pure_run_selector_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {"ML_PURE_RUN_ID": "20260308_164057", "ML_PURE_MODEL_GROUP": "banknifty_futures/h15_tp_auto"},
            clear=False,
        ):
            self.assertEqual(_resolve_ml_pure_run_id(None), "20260308_164057")
            self.assertEqual(_resolve_ml_pure_model_group(None), "banknifty_futures/h15_tp_auto")

    def test_resolve_ml_pure_numeric_values_use_default_when_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_resolve_ml_pure_int(None, "ML_PURE_MAX_HOLD_BARS", 15), 15)
            self.assertAlmostEqual(_resolve_ml_pure_float(None, "ML_PURE_MIN_OI", 50000.0), 50000.0, places=6)
            self.assertEqual(_resolve_ml_pure_int(None, "ML_PURE_MAX_FEATURE_AGE_SEC", 90), 90)
            self.assertEqual(_resolve_ml_pure_int(None, "ML_PURE_MAX_NAN_FEATURES", 3), 3)

    def test_build_engine_ml_pure_requires_model_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            logger = SignalLogger(root)
            threshold_report = self._write_threshold_report(root)
            with self.assertRaisesRegex(ValueError, "ml pure runtime requires --ml-pure-model-package"):
                build_engine(
                    engine_name="ml_pure",
                    min_confidence=0.0,
                    signal_logger=logger,
                    ml_pure_model_package=None,
                    ml_pure_threshold_report=str(threshold_report),
                )

    def test_build_engine_ml_pure_requires_threshold_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            logger = SignalLogger(root)
            model_bundle = self._write_model_bundle(root)
            with self.assertRaisesRegex(ValueError, "ml pure runtime requires --ml-pure-threshold-report"):
                build_engine(
                    engine_name="ml_pure",
                    min_confidence=0.0,
                    signal_logger=logger,
                    ml_pure_model_package=str(model_bundle),
                    ml_pure_threshold_report=None,
                )

    def test_build_engine_ml_pure_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            logger = SignalLogger(root)
            model_bundle = self._write_model_bundle(root)
            threshold_report = self._write_threshold_report(root)
            engine = build_engine(
                engine_name="ml_pure",
                min_confidence=0.0,
                signal_logger=logger,
                ml_pure_model_package=str(model_bundle),
                ml_pure_threshold_report=str(threshold_report),
            )
            self.assertIsInstance(engine, PureMLEngine)

    def test_ml_pure_switch_conflict_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "switch conflict"):
            _resolve_ml_pure_switch_paths(
                engine_key="ml_pure",
                run_id="20260308_164057",
                model_group="banknifty_futures/h15_tp_auto",
                model_package="model.joblib",
                threshold_report=None,
            )

    def test_ml_pure_switch_run_id_requires_model_group(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires --ml-pure-model-group"):
            _resolve_ml_pure_switch_paths(
                engine_key="ml_pure",
                run_id="20260308_164057",
                model_group=None,
                model_package=None,
                threshold_report=None,
            )

    def test_ml_pure_switch_explicit_paths_back_compat(self) -> None:
        model_path, threshold_path, meta = _resolve_ml_pure_switch_paths(
            engine_key="ml_pure",
            run_id=None,
            model_group=None,
            model_package="x.joblib",
            threshold_report="y.json",
        )
        self.assertEqual(model_path, "x.joblib")
        self.assertEqual(threshold_path, "y.json")
        self.assertIsNone(meta)


if __name__ == "__main__":
    unittest.main()
