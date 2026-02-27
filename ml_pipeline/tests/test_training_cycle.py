import unittest

import numpy as np
import pandas as pd

from ml_pipeline.training_cycle import PreprocessConfig, _filter_features_by_missing_rate, run_training_cycle


def _toy_labeled(days: int = 6, rows_per_day: int = 30) -> pd.DataFrame:
    rows = []
    start = pd.Timestamp("2023-06-12 09:15:00")
    for d in range(days):
        ts = pd.date_range(start + pd.Timedelta(days=d), periods=rows_per_day, freq="min")
        x1 = np.sin(np.linspace(0.0, 2.5, rows_per_day) + d * 0.1)
        x2 = np.cos(np.linspace(0.0, 2.5, rows_per_day) + d * 0.2)
        ce = (x1 + 0.2 * x2 > 0).astype(int)
        pe = (x2 - 0.2 * x1 > 0).astype(int)
        frame = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": [str(t.date()) for t in ts],
                "source_day": [str(ts[0].date())] * rows_per_day,
                "fut_symbol": ["BANKNIFTY-I"] * rows_per_day,
                "expiry_code": ["15JUN23"] * rows_per_day,
                "ce_symbol": ["BANKNIFTY15JUN2344100CE"] * rows_per_day,
                "pe_symbol": ["BANKNIFTY15JUN2344100PE"] * rows_per_day,
                "fut_open": 44000.0 + (x1 * 10.0),
                "fut_high": 44005.0 + (x1 * 10.0),
                "fut_low": 43995.0 + (x1 * 10.0),
                "fut_close": 44000.0 + (x1 * 12.0),
                "fut_oi": 100000.0 + np.arange(rows_per_day),
                "fut_volume": 3000.0 + np.arange(rows_per_day) * 10.0,
                "ret_1m": pd.Series(x1).diff().fillna(0.0).to_numpy(),
                "ret_3m": pd.Series(x1).diff(3).fillna(0.0).to_numpy(),
                "ema_9": pd.Series(x1).ewm(span=9, adjust=False).mean().to_numpy(),
                "ema_9_slope": pd.Series(x1).ewm(span=9, adjust=False).mean().diff().fillna(0.0).to_numpy(),
                "rsi_14": 50.0 + (x1 * 10.0),
                "atr_ratio": 0.002 + (np.abs(x2) * 0.001),
                "vwap_distance": x1 * 0.0008,
                "distance_from_day_high": x1 * 0.0005,
                "distance_from_day_low": x2 * 0.0005,
                "opt_0_ce_close": 120.0 + (x1 * 3.0),
                "opt_0_pe_close": 110.0 + (x2 * 3.0),
                "ce_oi_total": 500000.0 + np.arange(rows_per_day) * 100.0,
                "pe_oi_total": 510000.0 + np.arange(rows_per_day) * 100.0,
                "ce_volume_total": 200000.0 + np.arange(rows_per_day) * 50.0,
                "pe_volume_total": 190000.0 + np.arange(rows_per_day) * 50.0,
                "pcr_oi": 1.0 + (x2 * 0.01),
                "ce_pe_oi_diff": -10000.0 + (x1 * 120.0),
                "ce_pe_volume_diff": 10000.0 + (x2 * 80.0),
                "minute_of_day": ts.hour * 60 + ts.minute,
                "day_of_week": ts.dayofweek,
                "opening_range_breakout_up": (x1 > 0.6).astype(int),
                "opening_range_breakout_down": (x1 < -0.6).astype(int),
                "ce_label_valid": np.ones(rows_per_day),
                "pe_label_valid": np.ones(rows_per_day),
                "ce_label": ce,
                "pe_label": pe,
                "ce_forward_return": np.where(ce == 1, 0.01, -0.006),
                "pe_forward_return": np.where(pe == 1, 0.01, -0.006),
                "ce_entry_price": np.full(rows_per_day, 100.0),
                "ce_exit_price": np.where(ce == 1, 101.0, 99.4),
                "pe_entry_price": np.full(rows_per_day, 90.0),
                "pe_exit_price": np.where(pe == 1, 91.0, 89.4),
                "ce_mfe": np.full(rows_per_day, 0.012),
                "ce_mae": np.full(rows_per_day, -0.006),
                "pe_mfe": np.full(rows_per_day, 0.012),
                "pe_mae": np.full(rows_per_day, -0.006),
                "best_side_label": np.where(ce >= pe, 1, -1),
                "label_horizon_minutes": np.full(rows_per_day, 3),
                "label_return_threshold": np.full(rows_per_day, 0.002),
            }
        )
        rows.append(frame)
    return pd.concat(rows, ignore_index=True)


class TrainingCycleTests(unittest.TestCase):
    def test_missing_rate_filter(self) -> None:
        df = pd.DataFrame(
            {
                "a": [1.0, np.nan, 2.0, np.nan],
                "b": [1.0, 2.0, 3.0, 4.0],
                "c": [np.nan, np.nan, np.nan, np.nan],
            }
        )
        kept, dropped = _filter_features_by_missing_rate(df, ["a", "b", "c"], max_missing_rate=0.5)
        self.assertIn("a", kept)
        self.assertIn("b", kept)
        self.assertNotIn("c", kept)
        self.assertEqual(dropped[0]["feature"], "c")

    def test_training_cycle_smoke(self) -> None:
        df = _toy_labeled(days=6, rows_per_day=25)
        out = run_training_cycle(
            labeled_df=df,
            feature_profile="futures_options_only",
            objective="rmse",
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
            random_state=42,
            max_experiments=3,
            preprocess_cfg=PreprocessConfig(max_missing_rate=0.6, clip_lower_q=0.01, clip_upper_q=0.99),
        )
        report = out["report"]
        self.assertGreaterEqual(report["experiments_total"], 1)
        self.assertIn("best_experiment", report)
        self.assertIn("leaderboard", report)
        self.assertIn("preprocessing", report)
        self.assertIn("models", out["model_package"])
        self.assertIn("ce", out["model_package"]["models"])
        self.assertIn("pe", out["model_package"]["models"])


if __name__ == "__main__":
    unittest.main()
