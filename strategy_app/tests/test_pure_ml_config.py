import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd

from strategy_app.engines.pure_ml_engine import PureMLEngine
from strategy_app.logging.signal_logger import SignalLogger
from strategy_app.main import (
    _resolve_ml_pure_model_group,
    _resolve_ml_pure_float,
    _resolve_ml_pure_int,
    _resolve_ml_pure_model_package,
    _resolve_ml_pure_run_id,
    _resolve_ml_pure_switch_paths,
    _resolve_ml_pure_threshold_override,
    _resolve_ml_pure_threshold_report,
    build_engine,
)


class _ConstantProbModel:
    def __init__(self, prob: float) -> None:
        self._prob = float(prob)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = int(len(x))
        p1 = np.full(shape=(n,), fill_value=self._prob, dtype=float)
        p0 = 1.0 - p1
        return np.column_stack([p0, p1])


class PureMLConfigTests(unittest.TestCase):
    def _write_model_bundle(self, root: Path) -> Path:
        path = root / "model.joblib"
        bundle = {
            "feature_columns": ["ret_5m"],
            "models": {"ce": _ConstantProbModel(0.8), "pe": _ConstantProbModel(0.2)},
        }
        joblib.dump(bundle, path)
        return path

    def _write_threshold_report(self, root: Path) -> Path:
        path = root / "thresholds.json"
        path.write_text(json.dumps({"ce_threshold": 0.6, "pe_threshold": 0.6}), encoding="utf-8")
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

    def test_resolve_ml_pure_threshold_overrides_prefers_cli_then_env(self) -> None:
        with patch.dict("os.environ", {"ML_PURE_CE_THRESHOLD": "0.66"}, clear=False):
            self.assertAlmostEqual(_resolve_ml_pure_threshold_override(0.63, "ML_PURE_CE_THRESHOLD") or 0.0, 0.63, places=6)
            self.assertAlmostEqual(_resolve_ml_pure_threshold_override(None, "ML_PURE_CE_THRESHOLD") or 0.0, 0.66, places=6)

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
            self.assertAlmostEqual(_resolve_ml_pure_float(None, "ML_PURE_MIN_EDGE", 0.15), 0.15, places=6)

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
