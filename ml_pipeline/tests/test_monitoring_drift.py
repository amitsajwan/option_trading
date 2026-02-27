import unittest

import numpy as np
import pandas as pd

from ml_pipeline.monitoring_drift import (
    DriftThresholds,
    compute_psi,
    evaluate_alerts,
    run_drift_assessment,
)


class MonitoringDriftTests(unittest.TestCase):
    def test_psi_basic(self) -> None:
        ref = pd.Series(np.random.RandomState(0).normal(0, 1, 2000))
        cur_same = pd.Series(np.random.RandomState(1).normal(0, 1, 2000))
        cur_shift = pd.Series(np.random.RandomState(2).normal(2.0, 1, 2000))
        psi_same = compute_psi(ref, cur_same, bins=10)
        psi_shift = compute_psi(ref, cur_shift, bins=10)
        self.assertLess(psi_same, 0.1)
        self.assertGreater(psi_shift, 0.2)

    def test_feature_drift_alert_trigger(self) -> None:
        ref_feat = pd.DataFrame(
            {
                "a": np.random.RandomState(3).normal(0, 1, 1000),
                "b": np.random.RandomState(4).normal(0, 1, 1000),
            }
        )
        cur_feat = pd.DataFrame(
            {
                "a": np.random.RandomState(5).normal(2.5, 1, 1000),  # shifted
                "b": np.random.RandomState(6).normal(0, 1, 1000),
            }
        )
        ref_pred = pd.DataFrame(
            {
                "ce_prob": np.random.RandomState(7).uniform(0.2, 0.8, 500),
                "pe_prob": np.random.RandomState(8).uniform(0.2, 0.8, 500),
                "action": ["HOLD"] * 500,
            }
        )
        cur_pred = ref_pred.copy()
        report = run_drift_assessment(
            reference_features=ref_feat,
            current_features=cur_feat,
            reference_predictions=ref_pred,
            current_predictions=cur_pred,
            thresholds=DriftThresholds(),
        )
        self.assertIn(report["status"], {"warn", "alert"})
        self.assertTrue(any(a["type"] == "feature_drift" for a in report["alerts"]))

    def test_prediction_drift_alert_trigger(self) -> None:
        ref_feat = pd.DataFrame({"a": np.random.RandomState(9).normal(0, 1, 400)})
        cur_feat = ref_feat.copy()
        ref_pred = pd.DataFrame(
            {
                "ce_prob": np.random.RandomState(10).uniform(0.2, 0.4, 400),
                "pe_prob": np.random.RandomState(11).uniform(0.2, 0.4, 400),
                "action": ["HOLD"] * 400,
            }
        )
        cur_pred = pd.DataFrame(
            {
                "ce_prob": np.random.RandomState(12).uniform(0.8, 1.0, 400),
                "pe_prob": np.random.RandomState(13).uniform(0.8, 1.0, 400),
                "action": ["BUY_CE"] * 400,
            }
        )
        report = run_drift_assessment(
            reference_features=ref_feat,
            current_features=cur_feat,
            reference_predictions=ref_pred,
            current_predictions=cur_pred,
            thresholds=DriftThresholds(),
        )
        self.assertEqual(report["status"], "alert")
        self.assertTrue(any(a["type"] == "prediction_drift" for a in report["alerts"]))


if __name__ == "__main__":
    unittest.main()

