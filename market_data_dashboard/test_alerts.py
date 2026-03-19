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


if __name__ == "__main__":
    unittest.main()
