import unittest

import pandas as pd

from ml_pipeline.monitoring_execution import ExecutionDriftThresholds, evaluate_execution_drift


class MonitoringExecutionTests(unittest.TestCase):
    def test_event_mix_drift_alert(self) -> None:
        ref = pd.DataFrame(
            {
                "event_type": ["ENTRY", "MANAGE", "EXIT"] * 40,
                "event_reason": ["signal_entry", "hold", "time_stop"] * 40,
                "held_minutes": [0, 1, 3] * 40,
            }
        )
        cur = pd.DataFrame(
            {
                "event_type": ["ENTRY"] * 10 + ["EXIT"] * 110,
                "event_reason": ["signal_entry"] * 10 + ["signal_flip"] * 110,
                "held_minutes": [0] * 10 + [1] * 110,
            }
        )
        report = evaluate_execution_drift(ref, cur, thresholds=ExecutionDriftThresholds())
        self.assertEqual(report["status"], "alert")
        self.assertTrue(any(a["type"] == "event_mix_drift" for a in report["alerts"]))

    def test_hold_duration_drift_alert(self) -> None:
        ref = pd.DataFrame(
            {
                "event_type": ["EXIT"] * 100,
                "event_reason": ["time_stop"] * 100,
                "held_minutes": [2] * 100,
            }
        )
        cur = pd.DataFrame(
            {
                "event_type": ["EXIT"] * 100,
                "event_reason": ["time_stop"] * 100,
                "held_minutes": [8] * 100,
            }
        )
        report = evaluate_execution_drift(ref, cur, thresholds=ExecutionDriftThresholds())
        self.assertEqual(report["status"], "alert")
        self.assertTrue(any(a["type"] == "hold_duration_drift" for a in report["alerts"]))

    def test_no_alert_when_same_distribution(self) -> None:
        base = pd.DataFrame(
            {
                "event_type": ["ENTRY", "MANAGE", "EXIT", "IDLE"] * 20,
                "event_reason": ["signal_entry", "hold", "time_stop", "no_signal"] * 20,
                "held_minutes": [0, 1, 3, 0] * 20,
            }
        )
        report = evaluate_execution_drift(base, base.copy(), thresholds=ExecutionDriftThresholds())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(report["alerts"]), 0)


if __name__ == "__main__":
    unittest.main()
