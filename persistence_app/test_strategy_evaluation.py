import json
import os
import tempfile
import unittest
from unittest import mock

from persistence_app.strategy_evaluation import _trade_from_docs, rolling_ml_quality_from_collections


class CursorStub:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction):
        reverse = int(direction) < 0
        self._docs = sorted(self._docs, key=lambda row: row.get(key), reverse=reverse)
        return self

    def __iter__(self):
        return iter(self._docs)


class CollectionStub:
    def __init__(self, docs):
        self._docs = list(docs)

    def find(self, *_args, **_kwargs):
        return CursorStub(self._docs)


class StrategyEvaluationTests(unittest.TestCase):
    def _build_ml_quality_collections(self) -> tuple[CollectionStub, CollectionStub]:
        signals = CollectionStub(
            [
                {
                    "signal_id": "sig-ce",
                    "trade_date_ist": "2026-03-10",
                    "timestamp": "2026-03-10T09:30:00+05:30",
                    "engine_mode": "ml_pure",
                    "regime": "TRENDING",
                    "confidence": 0.73,
                    "reason": "[TRENDING] ML_PURE_STAGED: entry",
                    "decision_metrics": {
                        "entry_prob": 0.70,
                        "direction_up_prob": 0.67,
                        "ce_prob": 0.67,
                        "pe_prob": 0.33,
                        "recipe_prob": 0.82,
                        "recipe_margin": 0.08,
                    },
                    "payload": {"signal": {"contributing_strategies": ["ML_PURE_STAGED"]}},
                },
                {
                    "signal_id": "sig-pe",
                    "trade_date_ist": "2026-03-11",
                    "timestamp": "2026-03-11T09:30:00+05:30",
                    "engine_mode": "ml_pure",
                    "regime": "SIDEWAYS",
                    "confidence": 0.66,
                    "reason": "[SIDEWAYS] ML_PURE_STAGED: entry",
                    "decision_metrics": {
                        "entry_prob": 0.62,
                        "direction_up_prob": 0.35,
                        "ce_prob": 0.35,
                        "pe_prob": 0.65,
                        "recipe_prob": 0.71,
                        "recipe_margin": 0.05,
                    },
                    "payload": {"signal": {"contributing_strategies": ["ML_PURE_STAGED"]}},
                },
            ]
        )
        positions = CollectionStub(
            [
                {
                    "position_id": "pos-ce",
                    "signal_id": "sig-ce",
                    "event": "POSITION_OPEN",
                    "timestamp": "2026-03-10T09:30:00+05:30",
                    "trade_date_ist": "2026-03-10",
                    "engine_mode": "ml_pure",
                    "payload": {
                        "position": {
                            "signal_id": "sig-ce",
                            "timestamp": "2026-03-10T09:30:00+05:30",
                            "direction": "CE",
                            "entry_premium": 100.0,
                            "lots": 1,
                            "stop_loss_pct": 0.05,
                            "target_pct": 0.20,
                            "reason": "[TRENDING] ML_PURE_STAGED: entry",
                        }
                    },
                },
                {
                    "position_id": "pos-ce",
                    "event": "POSITION_CLOSE",
                    "timestamp": "2026-03-10T09:42:00+05:30",
                    "trade_date_ist": "2026-03-10",
                    "engine_mode": "ml_pure",
                    "actual_outcome": "win",
                    "actual_return_pct": 0.10,
                    "payload": {
                        "position": {
                            "timestamp": "2026-03-10T09:42:00+05:30",
                            "exit_premium": 110.0,
                            "pnl_pct": 0.10,
                            "mfe_pct": 0.12,
                            "mae_pct": -0.02,
                            "bars_held": 12,
                            "exit_reason": "TARGET_HIT",
                        }
                    },
                },
                {
                    "position_id": "pos-pe",
                    "signal_id": "sig-pe",
                    "event": "POSITION_OPEN",
                    "timestamp": "2026-03-11T09:30:00+05:30",
                    "trade_date_ist": "2026-03-11",
                    "engine_mode": "ml_pure",
                    "payload": {
                        "position": {
                            "signal_id": "sig-pe",
                            "timestamp": "2026-03-11T09:30:00+05:30",
                            "direction": "PE",
                            "entry_premium": 100.0,
                            "lots": 1,
                            "stop_loss_pct": 0.05,
                            "target_pct": 0.20,
                            "reason": "[SIDEWAYS] ML_PURE_STAGED: entry",
                        }
                    },
                },
                {
                    "position_id": "pos-pe",
                    "event": "POSITION_CLOSE",
                    "timestamp": "2026-03-11T09:41:00+05:30",
                    "trade_date_ist": "2026-03-11",
                    "engine_mode": "ml_pure",
                    "actual_outcome": "loss",
                    "actual_return_pct": -0.05,
                    "payload": {
                        "position": {
                            "timestamp": "2026-03-11T09:41:00+05:30",
                            "exit_premium": 95.0,
                            "pnl_pct": -0.05,
                            "mfe_pct": 0.02,
                            "mae_pct": -0.06,
                            "bars_held": 11,
                            "exit_reason": "STOP_LOSS",
                        }
                    },
                },
            ]
        )
        return signals, positions

    def test_trade_from_docs_carries_flattened_ml_metrics_and_actual_outcome(self) -> None:
        signal_map = {
            "sig-1": {
                "signal_id": "sig-1",
                "engine_mode": "ml_pure",
                "regime": "TRENDING",
                "confidence": 0.72,
                "reason": "[TRENDING] ML_PURE_STAGED: entry",
                "contributing_strategies": ["ML_PURE_STAGED"],
                "ml_entry_prob": 0.68,
                "ml_direction_up_prob": 0.74,
                "ml_ce_prob": 0.74,
                "ml_pe_prob": 0.26,
                "ml_recipe_prob": 0.81,
                "ml_recipe_margin": 0.11,
            }
        }
        docs = {
            "open": {
                "signal_id": "sig-1",
                "timestamp": "2026-03-10T09:30:00+05:30",
                "direction": "CE",
                "entry_premium": 100.0,
                "lots": 1,
                "stop_loss_pct": 0.05,
                "target_pct": 0.20,
                "reason": "[TRENDING] ML_PURE_STAGED: entry",
            },
            "open_doc": {
                "trade_date_ist": "2026-03-10",
            },
            "close": {
                "timestamp": "2026-03-10T09:42:00+05:30",
                "exit_premium": 112.0,
                "pnl_pct": 0.12,
                "mfe_pct": 0.15,
                "mae_pct": -0.02,
                "bars_held": 12,
                "exit_reason": "TARGET_HIT",
            },
            "close_doc": {
                "trade_date_ist": "2026-03-10",
                "actual_outcome": "win",
                "actual_return_pct": 0.12,
            },
        }

        trade = _trade_from_docs("pos-1", docs, signal_map)

        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade["engine_mode"], "ml_pure")
        self.assertEqual(trade["actual_outcome"], "win")
        self.assertAlmostEqual(float(trade["actual_return_pct"]), 0.12, places=6)
        self.assertAlmostEqual(float(trade["ml_entry_prob"]), 0.68, places=6)
        self.assertAlmostEqual(float(trade["ml_ce_prob"]), 0.74, places=6)
        self.assertAlmostEqual(float(trade["ml_pe_prob"]), 0.26, places=6)

    def test_rolling_ml_quality_from_collections_aggregates_metrics(self) -> None:
        signals, positions = self._build_ml_quality_collections()

        with tempfile.TemporaryDirectory() as tmpdir:
            threshold_path = f"{tmpdir}/threshold_report.json"
            with open(threshold_path, "w", encoding="utf-8") as handle:
                json.dump({"stage1": {"selected_threshold": 0.60}}, handle)
            summary = rolling_ml_quality_from_collections(
                signals,
                positions,
                window_trade_days=30,
                threshold_report_path=threshold_path,
            )

        self.assertEqual(summary["counts"]["ml_closed_trades"], 2)
        self.assertTrue(summary["stage1_precision"]["available"])
        self.assertEqual(summary["stage1_precision"]["source"], "threshold_report.stage1.selected_threshold")
        self.assertAlmostEqual(float(summary["stage1_precision"]["precision"]), 0.5, places=6)
        self.assertAlmostEqual(float(summary["stage1_precision"]["warning_threshold"]), 0.50, places=6)
        self.assertEqual(summary["stage1_precision"]["warning_threshold_source"], "default")
        self.assertAlmostEqual(float(summary["profit_factor"]["profit_factor"]), 2.0, places=6)
        self.assertAlmostEqual(float(summary["profit_factor"]["warning_threshold"]), 0.90, places=6)
        self.assertEqual(summary["profit_factor"]["warning_threshold_source"], "default")
        self.assertEqual(summary["window_dates"]["days_total"], 2)
        self.assertTrue(summary["stage2_ce_calibration"]["buckets"])
        self.assertAlmostEqual(float(summary["thresholds"]["profit_factor_warning"]["value"]), 0.90, places=6)
        self.assertEqual(summary["thresholds"]["profit_factor_warning"]["source"], "default")

    def test_rolling_ml_quality_marks_stage1_precision_unavailable_without_threshold_artifact(self) -> None:
        signals = CollectionStub([])
        positions = CollectionStub([])

        with mock.patch.dict("os.environ", {}, clear=False):
            old_value = os.environ.pop("ML_PURE_STAGE1_THRESHOLD", None)
            old_report = os.environ.pop("ML_PURE_THRESHOLD_REPORT", None)
            try:
                summary = rolling_ml_quality_from_collections(signals, positions, window_trade_days=30)
            finally:
                if old_value is not None:
                    os.environ["ML_PURE_STAGE1_THRESHOLD"] = old_value
                if old_report is not None:
                    os.environ["ML_PURE_THRESHOLD_REPORT"] = old_report

        self.assertFalse(summary["stage1_precision"]["available"])
        self.assertIsNone(summary["stage1_precision"]["threshold"])
        self.assertEqual(summary["stage1_precision"]["reason"], "missing_threshold_artifact")
        self.assertIsNone(summary["stage1_precision"]["precision"])

    def test_rolling_ml_quality_uses_env_configured_thresholds_for_breaches(self) -> None:
        signals, positions = self._build_ml_quality_collections()

        with tempfile.TemporaryDirectory() as tmpdir:
            threshold_path = f"{tmpdir}/threshold_report.json"
            training_summary_path = f"{tmpdir}/training_summary.json"
            with open(threshold_path, "w", encoding="utf-8") as handle:
                json.dump({"stage1": {"selected_threshold": 0.60}}, handle)
            with open(training_summary_path, "w", encoding="utf-8") as handle:
                json.dump({"training_regime_distribution": {"TRENDING": 1.0}}, handle)
            with mock.patch.dict(
                "os.environ",
                {
                    "LIVE_STRATEGY_ALERT_ML_PURE_STAGE1_PRECISION_WARN": "0.55",
                    "LIVE_STRATEGY_ALERT_ML_PURE_PROFIT_FACTOR_WARN": "2.50",
                    "LIVE_STRATEGY_ALERT_ML_PURE_REGIME_DRIFT_INFO": "0.60",
                },
                clear=False,
            ):
                summary = rolling_ml_quality_from_collections(
                    signals,
                    positions,
                    window_trade_days=30,
                    threshold_report_path=threshold_path,
                    training_summary_path=training_summary_path,
                )

        self.assertAlmostEqual(float(summary["thresholds"]["stage1_precision_warning"]["value"]), 0.55, places=6)
        self.assertEqual(summary["thresholds"]["stage1_precision_warning"]["source"], "env.LIVE_STRATEGY_ALERT_ML_PURE_STAGE1_PRECISION_WARN")
        self.assertAlmostEqual(float(summary["thresholds"]["profit_factor_warning"]["value"]), 2.50, places=6)
        self.assertEqual(summary["thresholds"]["profit_factor_warning"]["source"], "env.LIVE_STRATEGY_ALERT_ML_PURE_PROFIT_FACTOR_WARN")
        self.assertAlmostEqual(float(summary["thresholds"]["regime_drift_info"]["value"]), 0.60, places=6)
        self.assertEqual(summary["thresholds"]["regime_drift_info"]["source"], "env.LIVE_STRATEGY_ALERT_ML_PURE_REGIME_DRIFT_INFO")
        self.assertTrue(summary["breaches"]["stage1_precision_warning"])
        self.assertTrue(summary["breaches"]["profit_factor_warning"])
        self.assertFalse(summary["breaches"]["regime_drift_info"])


if __name__ == "__main__":
    unittest.main()
