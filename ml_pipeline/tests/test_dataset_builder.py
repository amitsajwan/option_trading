import unittest

import pandas as pd

from ml_pipeline.dataset_builder import build_canonical_day_panel, infer_strike_step, round_to_step
from ml_pipeline.raw_loader import DayRawData


class DatasetBuilderTests(unittest.TestCase):
    def test_strike_step_and_rounding(self) -> None:
        strikes = pd.Series([44000, 44100, 44200, 44300, 44400])
        step = infer_strike_step(strikes)
        self.assertEqual(step, 100)
        self.assertEqual(round_to_step(44142, step), 44100)
        self.assertEqual(round_to_step(44151, step), 44200)

    def test_canonical_panel_alignment(self) -> None:
        fut = pd.DataFrame(
            {
                "date": ["2023-06-15", "2023-06-15"],
                "time": ["09:15:00", "09:16:00"],
                "symbol": ["BANKNIFTY-I", "BANKNIFTY-I"],
                "open": [44000.0, 44100.0],
                "high": [44100.0, 44200.0],
                "low": [43950.0, 44050.0],
                "close": [44110.0, 44190.0],
                "oi": [1000, 1100],
                "volume": [100, 120],
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00", "2023-06-15 09:16:00"]),
            }
        )
        spot = pd.DataFrame(
            {
                "date": ["2023-06-15", "2023-06-15"],
                "time": ["09:15:00", "09:16:00"],
                "symbol": ["BANKNIFTY", "BANKNIFTY"],
                "open": [43990.0, 44090.0],
                "high": [44090.0, 44190.0],
                "low": [43940.0, 44040.0],
                "close": [44000.0, 44100.0],
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00", "2023-06-15 09:16:00"]),
            }
        )
        options = pd.DataFrame(
            {
                "date": ["2023-06-15"] * 4,
                "time": ["09:15:00"] * 4,
                "symbol": [
                    "BANKNIFTY15JUN2344100CE",
                    "BANKNIFTY15JUN2344100PE",
                    "BANKNIFTY15JUN2344200CE",
                    "BANKNIFTY15JUN2344200PE",
                ],
                "open": [100.0, 90.0, 80.0, 70.0],
                "high": [101.0, 91.0, 81.0, 71.0],
                "low": [99.0, 89.0, 79.0, 69.0],
                "close": [100.5, 90.5, 80.5, 70.5],
                "oi": [10000, 9000, 8000, 7000],
                "volume": [100, 90, 80, 70],
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00"] * 4),
                "expiry_code": ["15JUN23"] * 4,
                "strike": [44100, 44100, 44200, 44200],
                "option_type": ["CE", "PE", "CE", "PE"],
            }
        )
        raw = DayRawData(day="2023-06-15", fut=fut, options=options, spot=spot)
        panel = build_canonical_day_panel(raw)

        self.assertEqual(len(panel), len(fut))
        self.assertEqual(panel["timestamp"].nunique(), len(panel))
        self.assertTrue("opt_0_ce_close" in panel.columns)
        first = panel.iloc[0]
        self.assertAlmostEqual(first["spot_close"], 44000.0, places=6)
        self.assertTrue(pd.notna(first["opt_0_ce_close"]))

    def test_canonical_panel_with_depth_merge(self) -> None:
        fut = pd.DataFrame(
            {
                "date": ["2023-06-15", "2023-06-15"],
                "time": ["09:15:00", "09:16:00"],
                "symbol": ["BANKNIFTY-I", "BANKNIFTY-I"],
                "open": [44000.0, 44100.0],
                "high": [44100.0, 44200.0],
                "low": [43950.0, 44050.0],
                "close": [44110.0, 44190.0],
                "oi": [1000, 1100],
                "volume": [100, 120],
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00", "2023-06-15 09:16:00"]),
            }
        )
        spot = pd.DataFrame(
            {
                "date": ["2023-06-15", "2023-06-15"],
                "time": ["09:15:00", "09:16:00"],
                "symbol": ["BANKNIFTY", "BANKNIFTY"],
                "open": [43990.0, 44090.0],
                "high": [44090.0, 44190.0],
                "low": [43940.0, 44040.0],
                "close": [44000.0, 44100.0],
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00", "2023-06-15 09:16:00"]),
            }
        )
        options = pd.DataFrame(
            {
                "date": ["2023-06-15"] * 4,
                "time": ["09:15:00"] * 4,
                "symbol": [
                    "BANKNIFTY15JUN2344100CE",
                    "BANKNIFTY15JUN2344100PE",
                    "BANKNIFTY15JUN2344200CE",
                    "BANKNIFTY15JUN2344200PE",
                ],
                "open": [100.0, 90.0, 80.0, 70.0],
                "high": [101.0, 91.0, 81.0, 71.0],
                "low": [99.0, 89.0, 79.0, 69.0],
                "close": [100.5, 90.5, 80.5, 70.5],
                "oi": [10000, 9000, 8000, 7000],
                "volume": [100, 90, 80, 70],
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00"] * 4),
                "expiry_code": ["15JUN23"] * 4,
                "strike": [44100, 44100, 44200, 44200],
                "option_type": ["CE", "PE", "CE", "PE"],
            }
        )
        depth = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00"]),
                "trade_date": ["2023-06-15"],
                "depth_total_bid_qty": [2100.0],
                "depth_total_ask_qty": [1800.0],
                "depth_top_bid_qty": [1200.0],
                "depth_top_ask_qty": [950.0],
                "depth_top_bid_price": [44109.5],
                "depth_top_ask_price": [44110.5],
                "depth_spread": [1.0],
                "depth_imbalance": [0.0769],
            }
        )
        raw = DayRawData(day="2023-06-15", fut=fut, options=options, spot=spot)
        panel = build_canonical_day_panel(raw, depth_frame=depth)

        self.assertIn("depth_total_bid_qty", panel.columns)
        self.assertAlmostEqual(float(panel.iloc[0]["depth_total_bid_qty"]), 2100.0, places=6)
        self.assertTrue(pd.isna(panel.iloc[1]["depth_total_bid_qty"]))


if __name__ == "__main__":
    unittest.main()
