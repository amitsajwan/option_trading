import unittest

import pandas as pd

from ml_pipeline.quality_profiler import count_duplicates, count_missing_values, detect_iqr_outliers


class QualityProfilerUnitTests(unittest.TestCase):
    def test_missing_value_count(self) -> None:
        df = pd.DataFrame(
            {
                "open": [1.0, None, 3.0],
                "close": [1.1, 2.1, None],
            }
        )
        self.assertEqual(count_missing_values(df, ["open", "close"]), 2)

    def test_duplicate_count(self) -> None:
        df = pd.DataFrame(
            {
                "timestamp": ["2023-01-01 09:15:00", "2023-01-01 09:15:00", "2023-01-01 09:16:00"],
                "symbol": ["A", "A", "A"],
            }
        )
        self.assertEqual(count_duplicates(df, ["timestamp", "symbol"]), 2)

    def test_iqr_outlier_detection(self) -> None:
        df = pd.DataFrame({"close": [100, 101, 102, 103, 104, 5000]})
        outliers = detect_iqr_outliers(df, ["close"], multiplier=1.5)
        self.assertGreaterEqual(outliers, 1)


if __name__ == "__main__":
    unittest.main()

