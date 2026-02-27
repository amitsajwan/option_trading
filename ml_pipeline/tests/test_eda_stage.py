import unittest

import pandas as pd

from ml_pipeline.eda.stage import _pick_days, _split_by_day


class EdaStageTests(unittest.TestCase):
    def test_pick_days_uses_latest_n(self) -> None:
        days = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
        out = _pick_days(available_days=days, explicit_days=None, max_days=2)
        self.assertEqual(out, ["2024-01-03", "2024-01-04"])

    def test_pick_days_explicit_overrides_max(self) -> None:
        days = ["2024-01-01", "2024-01-02", "2024-01-03"]
        out = _pick_days(available_days=days, explicit_days="2024-01-01,2024-01-03", max_days=1)
        self.assertEqual(out, ["2024-01-01", "2024-01-03"])

    def test_split_by_day_time_ordered_non_overlap(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01 09:15:00", periods=12, freq="D"),
                "trade_date": [
                    "2024-01-01",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-04",
                    "2024-01-05",
                    "2024-01-05",
                    "2024-01-06",
                    "2024-01-06",
                ],
                "fut_close": [1.0] * 12,
            }
        )
        out = _split_by_day(frame, train_ratio=0.5, valid_ratio=0.25)
        train_days = set(out["train"]["trade_date"].astype(str).unique())
        valid_days = set(out["valid"]["trade_date"].astype(str).unique())
        eval_days = set(out["eval"]["trade_date"].astype(str).unique())
        self.assertTrue(train_days)
        self.assertTrue(valid_days)
        self.assertTrue(eval_days)
        self.assertEqual(train_days & valid_days, set())
        self.assertEqual(train_days & eval_days, set())
        self.assertEqual(valid_days & eval_days, set())


if __name__ == "__main__":
    unittest.main()
