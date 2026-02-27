import unittest

import numpy as np
import pandas as pd

from ml_pipeline.config import TrainConfig
from ml_pipeline.walk_forward import build_day_folds, run_walk_forward


def _make_labeled(days: int = 8, rows_per_day: int = 30) -> pd.DataFrame:
    all_rows = []
    start = pd.Timestamp("2023-01-02 09:15:00")
    for d in range(days):
        day_start = start + pd.Timedelta(days=d)
        ts = pd.date_range(day_start, periods=rows_per_day, freq="min")
        x1 = np.sin(np.linspace(0.1, 2.5, rows_per_day) + d * 0.3)
        x2 = np.cos(np.linspace(0.1, 2.5, rows_per_day) + d * 0.2)
        y_ce = (x1 + 0.2 * x2 > 0).astype(int)
        y_pe = (x2 - 0.1 * x1 > 0).astype(int)
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
                "ce_forward_return": np.where(y_ce == 1, 0.01, -0.005),
                "pe_forward_return": np.where(y_pe == 1, 0.012, -0.006),
                "ce_entry_price": np.full(rows_per_day, 100.0),
                "ce_exit_price": np.where(y_ce == 1, 101.0, 99.5),
                "pe_entry_price": np.full(rows_per_day, 80.0),
                "pe_exit_price": np.where(y_pe == 1, 81.0, 79.5),
                "ce_mfe": np.full(rows_per_day, 0.015),
                "ce_mae": np.full(rows_per_day, -0.004),
                "pe_mfe": np.full(rows_per_day, 0.017),
                "pe_mae": np.full(rows_per_day, -0.005),
                "best_side_label": np.where(y_ce >= y_pe, 1, -1),
                "label_horizon_minutes": np.full(rows_per_day, 3),
                "label_return_threshold": np.full(rows_per_day, 0.002),
            }
        )
        all_rows.append(block)
    return pd.concat(all_rows, ignore_index=True)


class WalkForwardTests(unittest.TestCase):
    def test_build_day_folds_chronology(self) -> None:
        days = [f"2023-01-{d:02d}" for d in range(2, 12)]
        folds = build_day_folds(days, train_days=4, valid_days=2, test_days=1, step_days=1)
        self.assertGreaterEqual(len(folds), 3)
        for fold in folds:
            tr = fold["train_days"]
            va = fold["valid_days"]
            te = fold["test_days"]
            self.assertTrue(max(tr) < min(va))
            self.assertTrue(max(va) < min(te))
            self.assertTrue(set(tr).isdisjoint(set(va)))
            self.assertTrue(set(tr).isdisjoint(set(te)))
            self.assertTrue(set(va).isdisjoint(set(te)))

    def test_walk_forward_smoke(self) -> None:
        df = _make_labeled(days=9, rows_per_day=25)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=7,
            max_depth=3,
            n_estimators=80,
            learning_rate=0.05,
        )
        report = run_walk_forward(
            labeled_df=df,
            config=cfg,
            train_days=4,
            valid_days=2,
            test_days=1,
            step_days=1,
        )
        self.assertGreaterEqual(report["ce"]["fold_count"], 2)
        self.assertGreaterEqual(report["pe"]["fold_count"], 2)
        self.assertIn("valid", report["ce"]["aggregate"])
        self.assertIn("test", report["ce"]["aggregate"])
        self.assertIn("f1_mean", report["ce"]["aggregate"]["valid"])
        self.assertIn("roc_auc_mean", report["pe"]["aggregate"]["test"])


if __name__ == "__main__":
    unittest.main()

