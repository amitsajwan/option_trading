import unittest

from ml_pipeline.temporal_splits import partition_days_with_reserve


class TemporalSplitTests(unittest.TestCase):
    def test_partition_with_auto_eval_end_and_reserve(self) -> None:
        days = [
            "2024-01-02",
            "2024-02-02",
            "2024-03-01",
            "2024-04-01",
            "2024-05-01",
            "2024-06-03",
        ]
        part = partition_days_with_reserve(
            days,
            lookback_years=1,
            evaluation_end_day=None,
            reserve_months=2,
        )
        self.assertEqual(part.evaluation_end_day, "2024-04-03")
        self.assertEqual(part.model_days, ["2024-01-02", "2024-02-02", "2024-03-01", "2024-04-01"])
        self.assertEqual(part.holdout_days, ["2024-05-01", "2024-06-03"])

    def test_partition_with_explicit_eval_end(self) -> None:
        days = [
            "2024-01-10",
            "2024-02-10",
            "2024-03-10",
            "2024-04-10",
            "2024-05-10",
            "2024-06-10",
            "2024-07-10",
        ]
        part = partition_days_with_reserve(
            days,
            lookback_years=1,
            evaluation_end_day="2024-04-10",
            reserve_months=2,
        )
        self.assertEqual(part.model_days, ["2024-01-10", "2024-02-10", "2024-03-10", "2024-04-10"])
        self.assertEqual(part.holdout_days, ["2024-05-10", "2024-06-10"])


if __name__ == "__main__":
    unittest.main()

