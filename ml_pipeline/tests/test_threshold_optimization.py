import unittest

import numpy as np
import pandas as pd

from ml_pipeline.config import DecisionConfig, TrainConfig
from ml_pipeline.threshold_optimization import (
    find_best_threshold,
    run_threshold_optimization,
    threshold_values,
)


def _synthetic_labeled(days: int = 8, rows_per_day: int = 24) -> pd.DataFrame:
    rows = []
    start = pd.Timestamp("2023-01-02 09:15:00")
    for d in range(days):
        ts = pd.date_range(start + pd.Timedelta(days=d), periods=rows_per_day, freq="min")
        x1 = np.sin(np.linspace(0.0, 2.0, rows_per_day) + d * 0.3)
        x2 = np.cos(np.linspace(0.0, 2.2, rows_per_day) + d * 0.2)
        y_ce = (x1 + 0.2 * x2 > 0).astype(int)
        y_pe = (x2 - 0.15 * x1 > 0).astype(int)
        ret_ce = np.where(y_ce == 1, 0.012, -0.007)
        ret_pe = np.where(y_pe == 1, 0.011, -0.006)
        block = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": [str(t.date()) for t in ts],
                "source_day": [str(ts[0].date())] * rows_per_day,
                "fut_symbol": ["BANKNIFTY-I"] * rows_per_day,
                "expiry_code": ["15JUN23"] * rows_per_day,
                "ce_symbol": ["BANKNIFTY15JUN2344000CE"] * rows_per_day,
                "pe_symbol": ["BANKNIFTY15JUN2344000PE"] * rows_per_day,
                "feature_a": x1,
                "feature_b": x2,
                "feature_c": np.linspace(-1.0, 1.0, rows_per_day),
                "minute_of_day": ts.hour * 60 + ts.minute,
                "ce_label_valid": np.ones(rows_per_day),
                "pe_label_valid": np.ones(rows_per_day),
                "ce_label": y_ce,
                "pe_label": y_pe,
                "ce_forward_return": ret_ce,
                "pe_forward_return": ret_pe,
                "ce_entry_price": np.full(rows_per_day, 100.0),
                "ce_exit_price": 100.0 * (1.0 + ret_ce),
                "pe_entry_price": np.full(rows_per_day, 80.0),
                "pe_exit_price": 80.0 * (1.0 + ret_pe),
                "ce_mfe": np.full(rows_per_day, 0.015),
                "ce_mae": np.full(rows_per_day, -0.004),
                "pe_mfe": np.full(rows_per_day, 0.017),
                "pe_mae": np.full(rows_per_day, -0.005),
                "best_side_label": np.where(y_ce >= y_pe, 1, -1),
                "label_horizon_minutes": np.full(rows_per_day, 3),
                "label_return_threshold": np.full(rows_per_day, 0.002),
            }
        )
        rows.append(block)
    return pd.concat(rows, ignore_index=True)


class ThresholdOptimizationTests(unittest.TestCase):
    def test_threshold_search_correctness(self) -> None:
        folds = [
            {
                "fold_ok": True,
                "valid": {
                    "prob": np.array([0.2, 0.4, 0.6, 0.8]),
                    "ret": np.array([-0.01, -0.005, 0.01, 0.02]),
                    "label": np.array([0, 0, 1, 1]),
                },
                "test": {
                    "prob": np.array([0.3, 0.7]),
                    "ret": np.array([-0.004, 0.012]),
                    "label": np.array([0, 1]),
                },
            }
        ]
        grid = threshold_values(0.3, 0.8, 0.1)
        result = find_best_threshold(folds, thresholds=grid, cost_per_trade=0.001)
        self.assertIsNotNone(result["best_threshold"])
        # With this setup, high thresholds remove losing trades and should dominate.
        self.assertGreaterEqual(result["best_threshold"], 0.6)

    def test_reproducibility_same_seed(self) -> None:
        df = _synthetic_labeled(days=9, rows_per_day=20)
        train_cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=17,
            max_depth=3,
            n_estimators=80,
            learning_rate=0.05,
        )
        decision_cfg = DecisionConfig(
            threshold_min=0.5,
            threshold_max=0.9,
            threshold_step=0.05,
            cost_per_trade=0.0006,
        )
        r1 = run_threshold_optimization(
            labeled_df=df,
            train_config=train_cfg,
            decision_config=decision_cfg,
            train_days=4,
            valid_days=2,
            test_days=1,
            step_days=1,
        )
        r2 = run_threshold_optimization(
            labeled_df=df,
            train_config=train_cfg,
            decision_config=decision_cfg,
            train_days=4,
            valid_days=2,
            test_days=1,
            step_days=1,
        )
        self.assertEqual(r1["ce"]["selected_threshold"], r2["ce"]["selected_threshold"])
        self.assertEqual(r1["pe"]["selected_threshold"], r2["pe"]["selected_threshold"])


if __name__ == "__main__":
    unittest.main()

