import unittest

import numpy as np
import pandas as pd

from ml_pipeline.config import DecisionConfig, TrainConfig
from ml_pipeline.depth_ablation import run_depth_ablation
from ml_pipeline.depth_dataset import build_depth_dataset_from_events


def _synthetic_labeled(days: int = 6, rows_per_day: int = 18, include_depth: bool = False) -> pd.DataFrame:
    blocks = []
    start = pd.Timestamp("2023-01-02 09:15:00")
    for d in range(days):
        ts = pd.date_range(start + pd.Timedelta(days=d), periods=rows_per_day, freq="min")
        idx = np.arange(rows_per_day)
        feature_a = np.sin(idx / 3.0 + d)
        feature_b = np.cos(idx / 4.0 + d * 0.5)
        y_ce = ((idx + d) % 2).astype(int)
        y_pe = ((idx + d + 1) % 2).astype(int)
        ce_ret = np.where(y_ce == 1, 0.010, -0.006)
        pe_ret = np.where(y_pe == 1, 0.011, -0.0065)
        block = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": [str(t.date()) for t in ts],
                "source_day": [str(ts[0].date())] * rows_per_day,
                "fut_symbol": ["BANKNIFTY-I"] * rows_per_day,
                "expiry_code": ["15JUN23"] * rows_per_day,
                "ce_symbol": ["BANKNIFTY15JUN2344000CE"] * rows_per_day,
                "pe_symbol": ["BANKNIFTY15JUN2344000PE"] * rows_per_day,
                "feature_a": feature_a,
                "feature_b": feature_b,
                "ce_label_valid": np.ones(rows_per_day),
                "pe_label_valid": np.ones(rows_per_day),
                "ce_label": y_ce,
                "pe_label": y_pe,
                "ce_forward_return": ce_ret,
                "pe_forward_return": pe_ret,
                "ce_entry_price": np.full(rows_per_day, 100.0),
                "ce_exit_price": 100.0 * (1.0 + ce_ret),
                "pe_entry_price": np.full(rows_per_day, 80.0),
                "pe_exit_price": 80.0 * (1.0 + pe_ret),
                "ce_mfe": np.full(rows_per_day, 0.014),
                "ce_mae": np.full(rows_per_day, -0.004),
                "pe_mfe": np.full(rows_per_day, 0.015),
                "pe_mae": np.full(rows_per_day, -0.0045),
                "label_horizon_minutes": np.full(rows_per_day, 3),
                "label_return_threshold": np.full(rows_per_day, 0.002),
                "best_side_label": np.where(y_ce >= y_pe, 1, -1),
                "opt_0_ce_close": np.full(rows_per_day, 100.0),
                "opt_0_ce_high": np.full(rows_per_day, 102.0),
                "opt_0_ce_low": np.full(rows_per_day, 99.0),
                "opt_0_ce_volume": np.full(rows_per_day, 1000.0),
                "opt_0_pe_close": np.full(rows_per_day, 80.0),
                "opt_0_pe_high": np.full(rows_per_day, 81.5),
                "opt_0_pe_low": np.full(rows_per_day, 79.5),
                "opt_0_pe_volume": np.full(rows_per_day, 800.0),
            }
        )
        if include_depth:
            block["depth_total_bid_qty"] = np.linspace(2000.0, 2300.0, rows_per_day)
            block["depth_total_ask_qty"] = np.linspace(1800.0, 2200.0, rows_per_day)
            block["depth_top_bid_qty"] = np.linspace(1000.0, 1200.0, rows_per_day)
            block["depth_top_ask_qty"] = np.linspace(900.0, 1100.0, rows_per_day)
            block["depth_top_bid_price"] = block["opt_0_ce_close"] - 0.5
            block["depth_top_ask_price"] = block["opt_0_ce_close"] + 0.5
            block["depth_spread"] = block["depth_top_ask_price"] - block["depth_top_bid_price"]
            block["depth_imbalance"] = (block["depth_total_bid_qty"] - block["depth_total_ask_qty"]) / (
                block["depth_total_bid_qty"] + block["depth_total_ask_qty"]
            )
        blocks.append(block)
    return pd.concat(blocks, ignore_index=True)


class DepthDatasetTests(unittest.TestCase):
    def test_build_depth_dataset_from_events(self) -> None:
        events = [
            {
                "timestamp": "2023-06-15T09:15:00+05:30",
                "depth": {
                    "total_bid_qty": 2100,
                    "total_ask_qty": 1800,
                    "top_bid_qty": 1200,
                    "top_ask_qty": 950,
                    "top_bid_price": 44109.5,
                    "top_ask_price": 44110.5,
                    "spread": 1.0,
                    "imbalance": 0.0769,
                },
            },
            {"timestamp": "2023-06-15T09:16:00+05:30", "depth": None},
        ]
        frame = build_depth_dataset_from_events(events)
        self.assertEqual(len(frame), 1)
        self.assertIn("depth_total_bid_qty", frame.columns)
        self.assertAlmostEqual(float(frame.iloc[0]["depth_total_bid_qty"]), 2100.0, places=6)

    def test_depth_ablation_handles_missing_depth_columns(self) -> None:
        df = _synthetic_labeled(include_depth=False)
        train_cfg = TrainConfig(random_state=7, max_depth=3, n_estimators=40, learning_rate=0.05)
        decision_cfg = DecisionConfig(threshold_min=0.5, threshold_max=0.8, threshold_step=0.1, cost_per_trade=0.0006)
        report = run_depth_ablation(
            labeled_df=df,
            train_config=train_cfg,
            decision_config=decision_cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
        )
        self.assertEqual(report["results"]["with_depth"]["status"], "no_depth_columns")
        self.assertEqual(report["results"]["baseline_no_depth"]["status"], "ok")

    def test_depth_ablation_smoke_with_depth_columns(self) -> None:
        df = _synthetic_labeled(include_depth=True)
        train_cfg = TrainConfig(random_state=9, max_depth=3, n_estimators=40, learning_rate=0.05)
        decision_cfg = DecisionConfig(threshold_min=0.5, threshold_max=0.8, threshold_step=0.1, cost_per_trade=0.0006)
        report = run_depth_ablation(
            labeled_df=df,
            train_config=train_cfg,
            decision_config=decision_cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
        )
        self.assertEqual(report["results"]["with_depth"]["status"], "ok")
        self.assertEqual(report["results"]["baseline_no_depth"]["status"], "ok")
        with_depth_metrics = report["results"]["with_depth"]["metrics"]
        base_metrics = report["results"]["baseline_no_depth"]["metrics"]
        self.assertIsNotNone(with_depth_metrics)
        self.assertIsNotNone(base_metrics)
        self.assertGreaterEqual(with_depth_metrics["feature_count"], base_metrics["feature_count"])


if __name__ == "__main__":
    unittest.main()
