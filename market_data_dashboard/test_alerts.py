import unittest
from unittest.mock import patch

from market_data_dashboard.ux.alerts import build_active_alerts


class AlertsTests(unittest.TestCase):
    def test_critical_freshness_with_open_positions(self) -> None:
        alerts = build_active_alerts(
            freshness={"votes_fresh": False, "signals_fresh": True, "positions_fresh": True},
            stale_open_positions=[],
            warnings=[],
            engine_context={"active_engine_mode": "ml_pure"},
            decision_diagnostics={
                "deterministic": {"counts": {"directional_entry_votes_day": 3}, "ratios": {"policy_block_rate_day": 0.2}},
                "ml_pure": {"ratios": {"hold_rate": 0.1}},
            },
            counts={"open_positions": 1},
            latest_decision={"reason_code": "risk_halt"},
            previous_engine_mode="ml_pure",
        )
        self.assertTrue(alerts)
        self.assertEqual(alerts[0]["id"], "data_stale_with_exposure")
        self.assertEqual(alerts[0]["severity"], "critical")

    def test_critical_freshness_with_only_stale_open_positions(self) -> None:
        alerts = build_active_alerts(
            freshness={"votes_fresh": False, "signals_fresh": True, "positions_fresh": True},
            stale_open_positions=[{"position_id": "pos-1"}],
            warnings=[],
            engine_context={"active_engine_mode": "ml_pure"},
            decision_diagnostics={
                "deterministic": {"counts": {"directional_entry_votes_day": 1}, "ratios": {"policy_block_rate_day": 0.1}},
                "ml_pure": {"ratios": {"hold_rate": 0.1}},
            },
            counts={"open_positions": 0},
            latest_decision=None,
            previous_engine_mode=None,
        )
        ids = {str(row.get("id") or "") for row in alerts}
        self.assertIn("data_stale_with_exposure", ids)

    def test_duplicate_alerts_aggregate_occurrences(self) -> None:
        alerts = build_active_alerts(
            freshness={"votes_fresh": True, "signals_fresh": True, "positions_fresh": True},
            stale_open_positions=[],
            warnings=["service_latency_warning", "service_latency_warning"],
            engine_context={"active_engine_mode": "ml_pure"},
            decision_diagnostics={
                "deterministic": {"counts": {"directional_entry_votes_day": 0}, "ratios": {}},
                "ml_pure": {"ratios": {}},
            },
            counts={"open_positions": 0},
            latest_decision=None,
            previous_engine_mode=None,
        )
        stale_alert = next((row for row in alerts if row.get("id") == "warning_service_latency_warning"), None)
        self.assertIsNotNone(stale_alert)
        self.assertEqual(stale_alert["occurrences"], 2)

    def test_threshold_override_suppresses_high_rate_alerts(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "LIVE_STRATEGY_ALERT_POLICY_BLOCK_RATE_WARN": "0.95",
                "LIVE_STRATEGY_ALERT_ML_PURE_HOLD_RATE_WARN": "0.95",
            },
            clear=False,
        ):
            alerts = build_active_alerts(
                freshness={"votes_fresh": True, "signals_fresh": True, "positions_fresh": True},
                stale_open_positions=[],
                warnings=[],
                engine_context={"active_engine_mode": "ml_pure"},
                decision_diagnostics={
                    "deterministic": {"counts": {"directional_entry_votes_day": 5}, "ratios": {"policy_block_rate_day": 0.90}},
                    "ml_pure": {"ratios": {"hold_rate": 0.90}},
                },
                counts={"open_positions": 0},
                latest_decision=None,
                previous_engine_mode=None,
            )
        ids = {str(row.get("id") or "") for row in alerts}
        self.assertNotIn("high_policy_block_rate", ids)
        self.assertNotIn("high_ml_pure_hold_rate", ids)

    def test_ml_pure_rolling_quality_alerts_surface_with_env_thresholds(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "LIVE_STRATEGY_ALERT_ML_PURE_STAGE1_PRECISION_WARN": "0.55",
                "LIVE_STRATEGY_ALERT_ML_PURE_PROFIT_FACTOR_WARN": "0.95",
                "LIVE_STRATEGY_ALERT_ML_PURE_REGIME_DRIFT_INFO": "0.22",
            },
            clear=False,
        ):
            alerts = build_active_alerts(
                freshness={"votes_fresh": True, "signals_fresh": True, "positions_fresh": True},
                stale_open_positions=[],
                warnings=[],
                engine_context={"active_engine_mode": "ml_pure"},
                decision_diagnostics={
                    "deterministic": {"counts": {"directional_entry_votes_day": 3}, "ratios": {"policy_block_rate_day": 0.1}},
                    "ml_pure": {
                        "ratios": {"hold_rate": 0.2},
                        "rolling_quality": {
                            "status": "ok",
                            "stage1_precision": {"precision": 0.54},
                            "profit_factor": {"profit_factor": 0.94},
                            "regime_drift": {"max_abs_shift": 0.23},
                        },
                    },
                },
                counts={"open_positions": 0},
                latest_decision=None,
                previous_engine_mode=None,
            )
        ids = {str(row.get("id") or "") for row in alerts}
        self.assertIn("ml_pure_stage1_precision_degraded", ids)
        self.assertIn("ml_pure_profit_factor_degraded", ids)
        self.assertIn("ml_pure_regime_drift", ids)

    def test_ml_pure_rolling_quality_alerts_prefer_persisted_thresholds_and_breaches(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "LIVE_STRATEGY_ALERT_ML_PURE_STAGE1_PRECISION_WARN": "0.95",
                "LIVE_STRATEGY_ALERT_ML_PURE_PROFIT_FACTOR_WARN": "9.99",
                "LIVE_STRATEGY_ALERT_ML_PURE_REGIME_DRIFT_INFO": "0.99",
            },
            clear=False,
        ):
            alerts = build_active_alerts(
                freshness={"votes_fresh": True, "signals_fresh": True, "positions_fresh": True},
                stale_open_positions=[],
                warnings=[],
                engine_context={"active_engine_mode": "ml_pure"},
                decision_diagnostics={
                    "deterministic": {"counts": {"directional_entry_votes_day": 3}, "ratios": {"policy_block_rate_day": 0.1}},
                    "ml_pure": {
                        "ratios": {"hold_rate": 0.2},
                        "rolling_quality": {
                            "status": "ok",
                            "thresholds": {
                                "stage1_precision_warning": {"value": 0.55, "source": "rolling_summary"},
                                "profit_factor_warning": {"value": 0.95, "source": "rolling_summary"},
                                "regime_drift_info": {"value": 0.22, "source": "rolling_summary"},
                            },
                            "stage1_precision": {"precision": 0.54},
                            "profit_factor": {"profit_factor": 0.94},
                            "regime_drift": {"max_abs_shift": 0.23},
                            "breaches": {
                                "stage1_precision_warning": True,
                                "profit_factor_warning": True,
                                "regime_drift_info": True,
                            },
                        },
                    },
                },
                counts={"open_positions": 0},
                latest_decision=None,
                previous_engine_mode=None,
            )
        alerts_by_id = {str(row.get("id") or ""): row for row in alerts}
        self.assertIn("ml_pure_stage1_precision_degraded", alerts_by_id)
        self.assertIn("ml_pure_profit_factor_degraded", alerts_by_id)
        self.assertIn("ml_pure_regime_drift", alerts_by_id)
        self.assertIn("rolling_summary", str(alerts_by_id["ml_pure_stage1_precision_degraded"].get("detail") or ""))
        self.assertIn("rolling_summary", str(alerts_by_id["ml_pure_profit_factor_degraded"].get("detail") or ""))
        self.assertIn("rolling_summary", str(alerts_by_id["ml_pure_regime_drift"].get("detail") or ""))

    def test_ml_pure_monitoring_failure_alert_surfaces(self) -> None:
        alerts = build_active_alerts(
            freshness={"votes_fresh": True, "signals_fresh": True, "positions_fresh": True},
            stale_open_positions=[],
            warnings=[],
            engine_context={"active_engine_mode": "ml_pure"},
            decision_diagnostics={
                "deterministic": {"counts": {"directional_entry_votes_day": 1}, "ratios": {"policy_block_rate_day": 0.1}},
                "ml_pure": {
                    "ratios": {"hold_rate": 0.2},
                    "rolling_quality": {
                        "status": "error",
                        "stage1_precision": {"available": False},
                        "profit_factor": {"available": False},
                        "regime_drift": {"available": False},
                        "error": {"type": "RuntimeError", "message": "mongo unavailable"},
                    },
                },
            },
            counts={"open_positions": 0},
            latest_decision=None,
            previous_engine_mode=None,
        )
        alert = next((row for row in alerts if row.get("id") == "ml_pure_monitoring_failure"), None)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["severity"], "warning")
        self.assertIn("mongo unavailable", str(alert.get("detail") or ""))

    def test_ml_pure_monitoring_unavailable_alert_surfaces(self) -> None:
        alerts = build_active_alerts(
            freshness={"votes_fresh": True, "signals_fresh": True, "positions_fresh": True},
            stale_open_positions=[],
            warnings=[],
            engine_context={"active_engine_mode": "ml_pure"},
            decision_diagnostics={
                "deterministic": {"counts": {"directional_entry_votes_day": 1}, "ratios": {"policy_block_rate_day": 0.1}},
                "ml_pure": {
                    "ratios": {"hold_rate": 0.2},
                    "rolling_quality": {
                        "status": "unavailable",
                        "reason": "positions_collection_missing",
                        "stage1_precision": {"available": False},
                        "profit_factor": {"available": False},
                        "regime_drift": {"available": False},
                        "breaches": {},
                    },
                },
            },
            counts={"open_positions": 0},
            latest_decision=None,
            previous_engine_mode=None,
        )
        alert = next((row for row in alerts if row.get("id") == "ml_pure_monitoring_unavailable"), None)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["severity"], "warning")
        self.assertIn("positions_collection_missing", str(alert.get("detail") or ""))


if __name__ == "__main__":
    unittest.main()
