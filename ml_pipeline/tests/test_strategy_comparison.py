import unittest

import numpy as np
import pandas as pd

from ml_pipeline.config import TrainConfig
from ml_pipeline.strategy_comparison import run_strategy_comparison


def _synthetic_labeled(days: int = 6, rows_per_day: int = 24) -> pd.DataFrame:
    blocks = []
    start = pd.Timestamp("2023-01-02 09:15:00")
    for d in range(days):
        ts = pd.date_range(start + pd.Timedelta(days=d), periods=rows_per_day, freq="min")
        x = np.sin(np.linspace(0.1, 2.3, rows_per_day) + d * 0.4)
        z = np.cos(np.linspace(0.1, 2.2, rows_per_day) + d * 0.2)
        y_ce = (x > 0).astype(int)
        y_pe = (z > 0).astype(int)
        ce_ret = np.where(y_ce == 1, 0.011, -0.007)
        pe_ret = np.where(y_pe == 1, 0.010, -0.0065)
        block = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": [str(t.date()) for t in ts],
                "source_day": [str(ts[0].date())] * rows_per_day,
                "fut_symbol": ["BANKNIFTY-I"] * rows_per_day,
                "expiry_code": ["15JUN23"] * rows_per_day,
                "ce_symbol": ["BANKNIFTY15JUN2344000CE"] * rows_per_day,
                "pe_symbol": ["BANKNIFTY15JUN2344000PE"] * rows_per_day,
                "feature_a": x,
                "feature_b": z,
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
                "ce_mfe": np.full(rows_per_day, 0.013),
                "ce_mae": np.full(rows_per_day, -0.004),
                "pe_mfe": np.full(rows_per_day, 0.013),
                "pe_mae": np.full(rows_per_day, -0.004),
                "label_horizon_minutes": np.full(rows_per_day, 3),
                "label_return_threshold": np.full(rows_per_day, 0.002),
                "best_side_label": np.where(y_ce >= y_pe, 1, -1),
            }
        )
        blocks.append(block)
    return pd.concat(blocks, ignore_index=True)


class StrategyComparisonTests(unittest.TestCase):
    def test_consistent_dataset_across_modes(self) -> None:
        df = _synthetic_labeled(days=6, rows_per_day=20)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=13,
            max_depth=3,
            n_estimators=70,
            learning_rate=0.05,
        )
        report = run_strategy_comparison(
            labeled_df=df,
            ce_threshold=0.55,
            pe_threshold=0.55,
            cost_values=[0.0006],
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
        )
        self.assertEqual(report["best_mode_default_cost"] in {"ce_only", "pe_only", "dual"}, True)
        rows = report["consistency_check"]["test_rows_total"]
        folds = report["consistency_check"]["fold_count"]
        self.assertGreater(rows, 0)
        self.assertGreater(folds, 0)
        for mode in ("ce_only", "pe_only", "dual"):
            summary = report["results"][mode]["0.0006"]["summary"]
            self.assertEqual(summary["test_rows_total"], rows)
            self.assertEqual(summary["fold_count"], folds)

    def test_deterministic_repeated_run(self) -> None:
        df = _synthetic_labeled(days=7, rows_per_day=18)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=21,
            max_depth=3,
            n_estimators=60,
            learning_rate=0.05,
        )
        r1 = run_strategy_comparison(
            labeled_df=df,
            ce_threshold=0.6,
            pe_threshold=0.6,
            cost_values=[0.0006, 0.001],
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
        )
        r2 = run_strategy_comparison(
            labeled_df=df,
            ce_threshold=0.6,
            pe_threshold=0.6,
            cost_values=[0.0006, 0.001],
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
        )
        self.assertEqual(r1["best_mode_default_cost"], r2["best_mode_default_cost"])
        self.assertEqual(r1["ranking_default_cost"], r2["ranking_default_cost"])
        self.assertEqual(r1["results"]["dual"]["0.0006"]["summary"], r2["results"]["dual"]["0.0006"]["summary"])


if __name__ == "__main__":
    unittest.main()

