import unittest

import numpy as np
import pandas as pd

from ml_pipeline.leakage_audit import detect_suspicious_features, synthetic_leakage_injection_check
from ml_pipeline.walk_forward import build_day_folds


def _make_labeled(days: int = 8, rows_per_day: int = 40) -> pd.DataFrame:
    rows = []
    start = pd.Timestamp("2023-01-02 09:15:00")
    for d in range(days):
        ts = pd.date_range(start + pd.Timedelta(days=d), periods=rows_per_day, freq="min")
        x1 = np.sin(np.linspace(0.0, 3.0, rows_per_day) + d * 0.2)
        x2 = np.cos(np.linspace(0.0, 2.0, rows_per_day) + d * 0.3)
        ce = (x1 + 0.1 * x2 > 0).astype(int)
        pe_wave = np.sin(np.linspace(0.0, 8.0, rows_per_day) + d * 0.35)
        pe = (pe_wave > 0).astype(int)
        frame = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": [str(t.date()) for t in ts],
                "source_day": [str(ts[0].date())] * rows_per_day,
                "fut_symbol": ["BANKNIFTY-I"] * rows_per_day,
                "expiry_code": ["15JUN23"] * rows_per_day,
                "ce_symbol": ["BANKNIFTY15JUN2344000CE"] * rows_per_day,
                "pe_symbol": ["BANKNIFTY15JUN2344000PE"] * rows_per_day,
                "fut_open": 44000.0 + x1 * 10.0,
                "fut_high": 44010.0 + x1 * 10.0,
                "fut_low": 43990.0 + x1 * 10.0,
                "fut_close": 44000.0 + x1 * 12.0,
                "fut_volume": 1000.0 + np.arange(rows_per_day),
                "fut_oi": 100000.0 + np.arange(rows_per_day),
                "ret_1m": pd.Series(x1).diff().fillna(0.0).to_numpy(),
                "ret_3m": pd.Series(x1).diff(3).fillna(0.0).to_numpy(),
                "ema_9": pd.Series(x1).ewm(span=9, adjust=False).mean().to_numpy(),
                "ema_9_slope": pd.Series(x1).ewm(span=9, adjust=False).mean().diff().fillna(0.0).to_numpy(),
                "rsi_14": 50.0 + x1 * 5.0,
                "atr_ratio": 0.001 + np.abs(x2) * 0.0005,
                "vwap_distance": x1 * 0.0005,
                "distance_from_day_high": x1 * 0.0002,
                "distance_from_day_low": x2 * 0.0002,
                "ce_oi_total": 20000.0 + np.arange(rows_per_day),
                "pe_oi_total": 21000.0 + np.arange(rows_per_day),
                "ce_volume_total": 5000.0 + np.arange(rows_per_day),
                "pe_volume_total": 4900.0 + np.arange(rows_per_day),
                "pcr_oi": 1.0 + x2 * 0.01,
                "ce_pe_oi_diff": -1000.0 + x1 * 100.0,
                "ce_pe_volume_diff": 100.0 + x2 * 20.0,
                "minute_of_day": ts.hour * 60 + ts.minute,
                "day_of_week": ts.dayofweek,
                "opening_range_high": 44100.0,
                "opening_range_low": 43900.0,
                "opening_range_ready": np.ones(rows_per_day),
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
                "ce_mfe": np.full(rows_per_day, 0.01),
                "ce_mae": np.full(rows_per_day, -0.005),
                "pe_mfe": np.full(rows_per_day, 0.01),
                "pe_mae": np.full(rows_per_day, -0.005),
                "best_side_label": np.where(ce >= pe, 1, -1),
                "label_horizon_minutes": np.full(rows_per_day, 3),
                "label_return_threshold": np.full(rows_per_day, 0.002),
            }
        )
        rows.append(frame)
    return pd.concat(rows, ignore_index=True)


class LeakageAuditTests(unittest.TestCase):
    def test_build_day_folds_with_purge_embargo(self) -> None:
        days = [f"2023-01-{d:02d}" for d in range(2, 20)]
        folds = build_day_folds(
            days=days,
            train_days=4,
            valid_days=2,
            test_days=2,
            step_days=1,
            purge_days=1,
            embargo_days=1,
        )
        self.assertGreaterEqual(len(folds), 2)
        for fold in folds:
            self.assertEqual(len(fold["purge_days"]), 1)
            self.assertEqual(len(fold["embargo_days"]), 1)
            self.assertTrue(max(fold["train_days"]) < min(fold["valid_days"]))
            self.assertTrue(max(fold["valid_days"]) < min(fold["test_days"]))

    def test_detect_suspicious_feature_names(self) -> None:
        cols = ["ret_1m", "ce_forward_return", "safe_feature", "pe_tp_hit"]
        bad = detect_suspicious_features(cols)
        self.assertIn("ce_forward_return", bad)
        self.assertIn("pe_tp_hit", bad)
        self.assertNotIn("ret_1m", bad)

    def test_synthetic_leakage_injection_detected(self) -> None:
        df = _make_labeled(days=8, rows_per_day=35)
        ce = synthetic_leakage_injection_check(df, side="ce")
        pe = synthetic_leakage_injection_check(df, side="pe")
        self.assertTrue(ce["detected"])
        self.assertTrue(pe["detected"])
        self.assertGreaterEqual(ce["injected_auc"], 0.95)
        self.assertGreaterEqual(pe["injected_auc"], 0.95)


if __name__ == "__main__":
    unittest.main()
