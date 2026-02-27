import unittest

from ml_pipeline.train_two_year_pipeline import _chunk_days, _window_days


class TrainTwoYearPipelineTests(unittest.TestCase):
    def test_window_days_two_years(self) -> None:
        days = [
            "2021-10-29",
            "2022-10-31",
            "2022-11-01",
            "2023-01-02",
            "2024-10-30",
            "2024-10-31",
        ]
        out = _window_days(days, lookback_years=2, end_day="2024-10-31")
        self.assertEqual(out[0], "2022-11-01")
        self.assertEqual(out[-1], "2024-10-31")
        self.assertNotIn("2022-10-31", out)

    def test_chunk_days(self) -> None:
        days = [f"2024-01-{d:02d}" for d in range(1, 11)]
        chunks = _chunk_days(days, chunk_size_days=4)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0], days[:4])
        self.assertEqual(chunks[1], days[4:8])
        self.assertEqual(chunks[2], days[8:])


if __name__ == "__main__":
    unittest.main()
