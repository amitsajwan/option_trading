import unittest

import numpy as np
import pandas as pd

from ml_pipeline.dataset_freeze import build_day_split, build_synthetic_live_feature_row, evaluate_dataset_freeze
from ml_pipeline.train_baseline import FEATURE_PROFILE_FUTURES_OPTIONS_ONLY, select_feature_columns


def _make_labeled_from_live_contract(days: int = 6, rows_per_day: int = 20) -> pd.DataFrame:
    live_row = build_synthetic_live_feature_row()
    start = pd.Timestamp("2023-06-15 09:15:00")
    rows = []
    for d in range(days):
        day_start = start + pd.Timedelta(days=d)
        ts_values = pd.date_range(day_start, periods=rows_per_day, freq="min")
        for i, ts in enumerate(ts_values):
            item = {}
            for key, value in live_row.items():
                if key == "timestamp":
                    item[key] = ts
                elif key == "trade_date":
                    item[key] = str(ts.date())
                else:
                    item[key] = value
            item["source_day"] = str(day_start.date())
            item["fut_symbol"] = "BANKNIFTY-I"
            item["ce_symbol"] = "BANKNIFTY29JUN2344200CE"
            item["pe_symbol"] = "BANKNIFTY29JUN2344200PE"
            item["ce_label_valid"] = 1.0
            item["pe_label_valid"] = 1.0
            item["ce_label"] = int((i + d) % 2 == 0)
            item["pe_label"] = int((i + d) % 2 != 0)
            item["ce_forward_return"] = 0.01 if item["ce_label"] == 1 else -0.005
            item["pe_forward_return"] = 0.01 if item["pe_label"] == 1 else -0.005
            item["ce_entry_price"] = 100.0
            item["ce_exit_price"] = 101.0 if item["ce_label"] == 1 else 99.5
            item["pe_entry_price"] = 90.0
            item["pe_exit_price"] = 91.0 if item["pe_label"] == 1 else 89.5
            item["ce_mfe"] = 0.015
            item["ce_mae"] = -0.004
            item["pe_mfe"] = 0.014
            item["pe_mae"] = -0.004
            item["best_side_label"] = 1 if item["ce_label"] >= item["pe_label"] else -1
            item["label_horizon_minutes"] = 3
            item["label_return_threshold"] = 0.002
            rows.append(item)
    return pd.DataFrame(rows)


class DatasetFreezeTests(unittest.TestCase):
    def test_build_day_split_chronological_non_overlap(self) -> None:
        days = [f"2023-06-{d:02d}" for d in range(10, 22)]
        split = build_day_split(days, train_ratio=0.7, valid_ratio=0.15)
        train_days = split["train_days"]
        valid_days = split["valid_days"]
        test_days = split["test_days"]
        self.assertGreater(len(train_days), 0)
        self.assertGreater(len(valid_days), 0)
        self.assertGreater(len(test_days), 0)
        self.assertTrue(max(train_days) < min(valid_days))
        self.assertTrue(max(valid_days) < min(test_days))
        self.assertTrue(set(train_days).isdisjoint(valid_days))
        self.assertTrue(set(train_days).isdisjoint(test_days))
        self.assertTrue(set(valid_days).isdisjoint(test_days))

    def test_futures_options_profile_excludes_spot_depth_basis(self) -> None:
        df = _make_labeled_from_live_contract(days=3, rows_per_day=8)
        df["spot_close"] = np.linspace(100.0, 103.0, len(df))
        df["depth_total_bid_qty"] = np.linspace(1000.0, 1200.0, len(df))
        df["basis"] = np.linspace(1.0, 2.0, len(df))
        cols = select_feature_columns(df, feature_profile=FEATURE_PROFILE_FUTURES_OPTIONS_ONLY)
        self.assertNotIn("spot_close", cols)
        self.assertNotIn("depth_total_bid_qty", cols)
        self.assertNotIn("basis", cols)

    def test_dataset_freeze_live_parity_for_futures_options_profile(self) -> None:
        df = _make_labeled_from_live_contract(days=6, rows_per_day=20)
        report = evaluate_dataset_freeze(
            labeled_df=df,
            feature_profile=FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
            train_ratio=0.7,
            valid_ratio=0.15,
        )
        self.assertEqual(report["task"], "T26")
        self.assertTrue(report["parity"]["train_eval"]["parity_ok"])
        self.assertTrue(report["parity"]["live"]["parity_ok"])
        self.assertEqual(len(report["parity"]["live"]["missing_in_live_contract"]), 0)
        self.assertGreater(report["feature_set"]["feature_count"], 0)


if __name__ == "__main__":
    unittest.main()
