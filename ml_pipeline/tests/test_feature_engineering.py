import unittest
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from ml_pipeline.feature.engineering import build_feature_table


def _make_panel(rows: int = 40) -> pd.DataFrame:
    ts = pd.date_range("2023-06-15 09:15:00", periods=rows, freq="min")
    base_price = np.linspace(44000.0, 44200.0, rows)
    panel = pd.DataFrame(
        {
            "timestamp": ts,
            "trade_date": [str(t.date()) for t in ts],
            "source_day": [str(ts[0].date())] * rows,
            "fut_symbol": ["BANKNIFTY-I"] * rows,
            "fut_open": base_price - 5.0,
            "fut_high": base_price + 10.0,
            "fut_low": base_price - 10.0,
            "fut_close": base_price,
            "fut_oi": np.linspace(1000000, 1005000, rows),
            "fut_volume": np.linspace(1000, 5000, rows),
            "spot_open": base_price - 10.0,
            "spot_high": base_price + 7.0,
            "spot_low": base_price - 12.0,
            "spot_close": base_price - 4.0,
            "expiry_code": ["15JUN23"] * rows,
            "strike_step": [100] * rows,
            "atm_strike": ((base_price / 100).round() * 100).astype(int),
            "ce_oi_total": np.linspace(2000000, 2100000, rows),
            "pe_oi_total": np.linspace(1900000, 2150000, rows),
            "ce_volume_total": np.linspace(100000, 150000, rows),
            "pe_volume_total": np.linspace(90000, 160000, rows),
            "pcr_oi": np.linspace(0.9, 1.1, rows),
            "options_rows": [35000.0] * rows,
            "opt_m1_ce_open": np.linspace(90, 110, rows),
            "opt_m1_ce_high": np.linspace(91, 111, rows),
            "opt_m1_ce_low": np.linspace(89, 109, rows),
            "opt_m1_ce_close": np.linspace(90.5, 110.5, rows),
            "opt_m1_ce_oi": np.linspace(50000, 60000, rows),
            "opt_m1_ce_volume": np.linspace(5000, 7000, rows),
            "opt_m1_pe_open": np.linspace(85, 105, rows),
            "opt_m1_pe_high": np.linspace(86, 106, rows),
            "opt_m1_pe_low": np.linspace(84, 104, rows),
            "opt_m1_pe_close": np.linspace(85.5, 105.5, rows),
            "opt_m1_pe_oi": np.linspace(45000, 61000, rows),
            "opt_m1_pe_volume": np.linspace(5200, 7200, rows),
            "opt_0_ce_open": np.linspace(100, 140, rows),
            "opt_0_ce_high": np.linspace(101, 141, rows),
            "opt_0_ce_low": np.linspace(99, 139, rows),
            "opt_0_ce_close": np.linspace(100.5, 140.5, rows),
            "opt_0_ce_oi": np.linspace(70000, 80000, rows),
            "opt_0_ce_volume": np.linspace(8000, 10000, rows),
            "opt_0_pe_open": np.linspace(95, 135, rows),
            "opt_0_pe_high": np.linspace(96, 136, rows),
            "opt_0_pe_low": np.linspace(94, 134, rows),
            "opt_0_pe_close": np.linspace(95.5, 135.5, rows),
            "opt_0_pe_oi": np.linspace(65000, 82000, rows),
            "opt_0_pe_volume": np.linspace(8300, 10300, rows),
            "opt_p1_ce_open": np.linspace(80, 100, rows),
            "opt_p1_ce_high": np.linspace(81, 101, rows),
            "opt_p1_ce_low": np.linspace(79, 99, rows),
            "opt_p1_ce_close": np.linspace(80.5, 100.5, rows),
            "opt_p1_ce_oi": np.linspace(40000, 50000, rows),
            "opt_p1_ce_volume": np.linspace(4000, 5000, rows),
            "opt_p1_pe_open": np.linspace(78, 98, rows),
            "opt_p1_pe_high": np.linspace(79, 99, rows),
            "opt_p1_pe_low": np.linspace(77, 97, rows),
            "opt_p1_pe_close": np.linspace(78.5, 98.5, rows),
            "opt_p1_pe_oi": np.linspace(41000, 51000, rows),
            "opt_p1_pe_volume": np.linspace(4200, 5200, rows),
        }
    )
    return panel


class FeatureEngineeringTests(unittest.TestCase):
    def test_feature_columns_created(self) -> None:
        panel = _make_panel()
        features = build_feature_table(panel)
        expected = {
            "ret_1m",
            "ret_3m",
            "ema_9",
            "ema_21",
            "ema_9_21_spread",
            "rsi_14",
            "atr_14",
            "vwap_distance",
            "atm_call_return_1m",
            "ce_pe_oi_diff",
            "minute_of_day",
            "opening_range_breakout_up",
        }
        missing = [col for col in expected if col not in features.columns]
        self.assertEqual(missing, [])
        self.assertEqual(len(features), len(panel))

    def test_no_future_leakage_for_past_rows(self) -> None:
        panel = _make_panel()
        base = build_feature_table(panel)

        mutated = panel.copy()
        mutated.loc[30, "fut_close"] = mutated.loc[30, "fut_close"] * 2.0
        changed = build_feature_table(mutated)

        for col in ["ret_1m", "ema_9", "rsi_14", "atr_14", "vwap_distance"]:
            pd.testing.assert_series_equal(
                base.loc[:29, col].reset_index(drop=True),
                changed.loc[:29, col].reset_index(drop=True),
                check_names=False,
            )

    def test_depth_feature_columns_created_when_depth_present(self) -> None:
        panel = _make_panel()
        panel["depth_total_bid_qty"] = np.linspace(2000.0, 2200.0, len(panel))
        panel["depth_total_ask_qty"] = np.linspace(1800.0, 2100.0, len(panel))
        panel["depth_top_bid_qty"] = np.linspace(1000.0, 1200.0, len(panel))
        panel["depth_top_ask_qty"] = np.linspace(900.0, 1100.0, len(panel))
        panel["depth_top_bid_price"] = panel["fut_close"] - 0.5
        panel["depth_top_ask_price"] = panel["fut_close"] + 0.5
        features = build_feature_table(panel)

        expected = {
            "depth_bid_ask_ratio",
            "depth_imbalance",
            "depth_top_level_ratio",
            "depth_spread",
            "depth_spread_bps",
            "depth_imbalance_change_1m",
        }
        missing = [col for col in expected if col not in features.columns]
        self.assertEqual(missing, [])
        self.assertGreater(features["depth_spread"].dropna().mean(), 0.0)

    def test_dte_and_vix_features_created(self) -> None:
        panel = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2023-06-14 09:15:00",
                        "2023-06-14 09:16:00",
                        "2023-06-15 09:15:00",
                        "2023-06-15 09:16:00",
                    ]
                ),
                "trade_date": ["2023-06-14", "2023-06-14", "2023-06-15", "2023-06-15"],
                "source_day": ["2023-06-14", "2023-06-14", "2023-06-15", "2023-06-15"],
                "fut_symbol": ["BANKNIFTY-I"] * 4,
                "fut_open": [44000.0, 44005.0, 44100.0, 44110.0],
                "fut_high": [44010.0, 44015.0, 44110.0, 44120.0],
                "fut_low": [43990.0, 43995.0, 44090.0, 44100.0],
                "fut_close": [44005.0, 44010.0, 44105.0, 44115.0],
                "fut_oi": [1000.0, 1001.0, 1002.0, 1003.0],
                "fut_volume": [100.0, 110.0, 120.0, 130.0],
                "spot_open": [43995.0, 44000.0, 44095.0, 44105.0],
                "spot_high": [44005.0, 44010.0, 44105.0, 44115.0],
                "spot_low": [43985.0, 43990.0, 44085.0, 44095.0],
                "spot_close": [44000.0, 44005.0, 44100.0, 44110.0],
                "expiry_code": ["15JUN23", "15JUN23", "15JUN23", "15JUN23"],
                "strike_step": [100, 100, 100, 100],
                "atm_strike": [44000, 44000, 44100, 44100],
                "ce_oi_total": [10000.0, 10010.0, 10020.0, 10030.0],
                "pe_oi_total": [11000.0, 11010.0, 11020.0, 11030.0],
                "ce_volume_total": [500.0, 510.0, 520.0, 530.0],
                "pe_volume_total": [600.0, 610.0, 620.0, 630.0],
                "pcr_oi": [1.1, 1.1, 1.1, 1.1],
                "options_rows": [1000.0, 1000.0, 1000.0, 1000.0],
                "opt_0_ce_close": [100.0, 101.0, 102.0, 103.0],
                "opt_0_ce_oi": [2000.0, 2001.0, 2002.0, 2003.0],
                "opt_0_pe_close": [120.0, 119.0, 118.0, 117.0],
                "opt_0_pe_oi": [2100.0, 2101.0, 2102.0, 2103.0],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "vix.csv"
            csv_path.write_text(
                "Date ,Open ,High ,Low ,Close ,Prev. Close ,Change ,% Change\n"
                "13-JUN-2023,11,12,10,12,11,1,9.0\n"
                "14-JUN-2023,12,13,11,13,12,1,8.3\n"
                "15-JUN-2023,13,14,12,14,13,1,7.6\n",
                encoding="utf-8",
            )
            features = build_feature_table(panel, vix_source=csv_path)
        self.assertIn("dte_days", features.columns)
        self.assertIn("vix_prev_close", features.columns)
        dte_by_day = features.groupby("trade_date")["dte_days"].first().to_dict()
        self.assertEqual(int(dte_by_day["2023-06-14"]), 1)
        self.assertEqual(int(dte_by_day["2023-06-15"]), 0)
        vix_prev = features.groupby("trade_date")["vix_prev_close"].first().to_dict()
        self.assertAlmostEqual(float(vix_prev["2023-06-14"]), 12.0, places=6)
        self.assertAlmostEqual(float(vix_prev["2023-06-15"]), 13.0, places=6)


if __name__ == "__main__":
    unittest.main()

