import unittest

import numpy as np
import pandas as pd

from ml_pipeline.label_validation import build_breakout_alternative_labels, compute_forward_return_by_day, run_label_horizon_validation


def _base_frame() -> pd.DataFrame:
    ts = pd.to_datetime(
        [
            "2024-01-01 09:15:00",
            "2024-01-01 09:16:00",
            "2024-01-01 09:17:00",
            "2024-01-01 09:18:00",
            "2024-01-02 09:15:00",
            "2024-01-02 09:16:00",
            "2024-01-02 09:17:00",
            "2024-01-02 09:18:00",
        ]
    )
    fut = [100.0, 102.0, 104.0, 103.0, 200.0, 198.0, 196.0, 197.0]
    return pd.DataFrame(
        {
            "timestamp": ts,
            "trade_date": [str(t.date()) for t in ts],
            "fut_close": fut,
            "opening_range_ready": [1, 1, 1, 1, 1, 1, 1, 1],
            "opening_range_breakout_up": [1, 0, 0, 0, 0, 0, 0, 0],
            "opening_range_breakout_down": [0, 0, 0, 0, 1, 0, 0, 0],
            "label_horizon_minutes": [3] * len(ts),
            "ce_label_valid": [1.0] * len(ts),
            "pe_label_valid": [1.0] * len(ts),
            "ce_label": [1.0, 0.0, 0.0, np.nan, 0.0, 0.0, 0.0, np.nan],
            "pe_label": [0.0, 0.0, 0.0, np.nan, 1.0, 0.0, 0.0, np.nan],
        }
    )


class LabelValidationTests(unittest.TestCase):
    def test_forward_return_by_day_no_cross_day_leak(self) -> None:
        df = _base_frame()
        fr = compute_forward_return_by_day(df, horizon_minutes=2)
        # 2024-01-01 row0 => (104-100)/100
        self.assertAlmostEqual(float(fr.iloc[0]), 0.04, places=10)
        # day boundary rows without enough horizon should be NaN, not cross-day filled
        self.assertTrue(np.isnan(fr.iloc[3]))
        self.assertTrue(np.isnan(fr.iloc[7]))

    def test_breakout_label_forward_window_correctness(self) -> None:
        df = _base_frame()
        alt = build_breakout_alternative_labels(df, horizon_minutes=3, return_threshold=0.002)
        # Row 0 has breakout_up and positive 3m return => CE breakout label = 1
        self.assertEqual(float(alt.iloc[0]["ce_breakout_label"]), 1.0)
        # Row 4 has breakout_down and negative 3m return => PE breakout label = 1
        self.assertEqual(float(alt.iloc[4]["pe_breakout_label"]), 1.0)

    def test_no_lookahead_post_horizon_change_does_not_affect_label(self) -> None:
        df_a = _base_frame()
        df_b = _base_frame()
        # mutate post-horizon value for row 0 (h=2 uses idx 2; idx 3 should not affect)
        df_b.loc[df_b.index == 3, "fut_close"] = 9999.0
        alt_a = build_breakout_alternative_labels(df_a, horizon_minutes=2, return_threshold=0.002)
        alt_b = build_breakout_alternative_labels(df_b, horizon_minutes=2, return_threshold=0.002)
        self.assertAlmostEqual(float(alt_a.iloc[0]["fut_forward_return_h"]), float(alt_b.iloc[0]["fut_forward_return_h"]), places=12)
        self.assertEqual(float(alt_a.iloc[0]["ce_breakout_label"]), float(alt_b.iloc[0]["ce_breakout_label"]))

    def test_run_label_validation_report_shape(self) -> None:
        df = _base_frame()
        _, report = run_label_horizon_validation(df, horizon_minutes=3, return_threshold=0.002)
        self.assertEqual(report["task"], "T27")
        self.assertIn("base_label_validation", report)
        self.assertIn("breakout_alternative", report)


if __name__ == "__main__":
    unittest.main()
