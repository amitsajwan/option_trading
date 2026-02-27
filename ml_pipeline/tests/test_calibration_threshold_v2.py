import unittest

import numpy as np
import pandas as pd

from ml_pipeline.calibration_threshold_v2 import run_calibration_threshold_v2
from ml_pipeline.config import DecisionConfig, TrainConfig


def _make_labeled(days: int = 10, rows_per_day: int = 40) -> pd.DataFrame:
    rows = []
    start = pd.Timestamp("2023-01-02 09:15:00")
    for d in range(days):
        ts = pd.date_range(start + pd.Timedelta(days=d), periods=rows_per_day, freq="min")
        x1 = np.sin(np.linspace(0.0, 4.0, rows_per_day) + d * 0.15)
        x2 = np.cos(np.linspace(0.0, 3.0, rows_per_day) + d * 0.17)
        ce = (x1 + 0.15 * x2 > 0).astype(int)
        pe = (x2 - 0.15 * x1 > 0).astype(int)
        frame = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": [str(t.date()) for t in ts],
                "source_day": [str(ts[0].date())] * rows_per_day,
                "fut_symbol": ["BANKNIFTY-I"] * rows_per_day,
                "expiry_code": ["15JUN23"] * rows_per_day,
                "ce_symbol": ["BANKNIFTY15JUN2344000CE"] * rows_per_day,
                "pe_symbol": ["BANKNIFTY15JUN2344000PE"] * rows_per_day,
                "fut_open": 44000.0 + x1 * 9.0,
                "fut_high": 44008.0 + x1 * 9.0,
                "fut_low": 43992.0 + x1 * 9.0,
                "fut_close": 44000.0 + x1 * 10.0,
                "fut_volume": 1000.0 + np.arange(rows_per_day),
                "fut_oi": 100000.0 + np.arange(rows_per_day),
                "ret_1m": pd.Series(x1).diff().fillna(0.0).to_numpy(),
                "ret_3m": pd.Series(x1).diff(3).fillna(0.0).to_numpy(),
                "ema_9": pd.Series(x1).ewm(span=9, adjust=False).mean().to_numpy(),
                "ema_21": pd.Series(x1).ewm(span=21, adjust=False).mean().to_numpy(),
                "ema_9_slope": pd.Series(x1).ewm(span=9, adjust=False).mean().diff().fillna(0.0).to_numpy(),
                "rsi_14": 50.0 + x1 * 5.0,
                "atr_14": 8.0 + np.abs(x2) * 2.0,
                "atr_ratio": 0.001 + np.abs(x2) * 0.0003,
                "atr_percentile": np.clip(np.linspace(0.1, 0.9, rows_per_day), 0.0, 1.0),
                "fut_vwap": 44000.0 + x2 * 4.0,
                "vwap_distance": x1 * 0.0004,
                "distance_from_day_high": x1 * 0.0003,
                "distance_from_day_low": x2 * 0.0003,
                "strike_step": np.full(rows_per_day, 100.0),
                "atm_strike": np.full(rows_per_day, 44000.0),
                "ce_oi_total": 20000.0 + np.arange(rows_per_day),
                "pe_oi_total": 21000.0 + np.arange(rows_per_day),
                "ce_volume_total": 5000.0 + np.arange(rows_per_day),
                "pe_volume_total": 4900.0 + np.arange(rows_per_day),
                "pcr_oi": 1.0 + x2 * 0.01,
                "ce_pe_oi_diff": -1000.0 + x1 * 100.0,
                "ce_pe_volume_diff": 100.0 + x2 * 20.0,
                "opt_0_ce_open": 100.0 + x1,
                "opt_0_ce_high": 101.0 + x1,
                "opt_0_ce_low": 99.0 + x1,
                "opt_0_ce_close": 100.5 + x1,
                "opt_0_ce_oi": 1000.0 + np.arange(rows_per_day),
                "opt_0_ce_volume": 300.0 + np.arange(rows_per_day),
                "opt_0_pe_open": 90.0 + x2,
                "opt_0_pe_high": 91.0 + x2,
                "opt_0_pe_low": 89.0 + x2,
                "opt_0_pe_close": 90.5 + x2,
                "opt_0_pe_oi": 1100.0 + np.arange(rows_per_day),
                "opt_0_pe_volume": 280.0 + np.arange(rows_per_day),
                "minute_of_day": ts.hour * 60 + ts.minute,
                "day_of_week": ts.dayofweek,
                "minute_index": np.arange(rows_per_day),
                "opening_range_high": np.full(rows_per_day, 44100.0),
                "opening_range_low": np.full(rows_per_day, 43900.0),
                "opening_range_ready": np.ones(rows_per_day),
                "opening_range_breakout_up": (x1 > 0.6).astype(int),
                "opening_range_breakout_down": (x1 < -0.6).astype(int),
                "ce_label_valid": np.ones(rows_per_day),
                "pe_label_valid": np.ones(rows_per_day),
                "ce_label": ce,
                "pe_label": pe,
                "ce_forward_return": np.where(ce == 1, 0.010, -0.006),
                "pe_forward_return": np.where(pe == 1, 0.010, -0.006),
                "ce_path_exit_reason": np.where(ce == 1, "tp", "sl"),
                "pe_path_exit_reason": np.where(pe == 1, "tp", "sl"),
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


class CalibrationThresholdV2Tests(unittest.TestCase):
    def test_run_structure(self) -> None:
        df = _make_labeled(days=9, rows_per_day=35)
        report = run_calibration_threshold_v2(
            labeled_df=df,
            train_cfg=TrainConfig(random_state=7, max_depth=3, n_estimators=80, learning_rate=0.05),
            decision_cfg=DecisionConfig(threshold_min=0.5, threshold_max=0.8, threshold_step=0.1, cost_per_trade=0.0006),
            feature_profile="futures_options_only",
            train_days=4,
            valid_days=2,
            test_days=1,
            step_days=1,
            purge_days=1,
            embargo_days=0,
            reliability_bins=8,
        )
        self.assertEqual(report["task"], "T31")
        self.assertIn(report["ce"]["selected_calibration_method"], {"identity", "platt", "isotonic"})
        self.assertIn(report["pe"]["selected_calibration_method"], {"identity", "platt", "isotonic"})
        self.assertIsNotNone(report["dual_mode_policy"]["ce_threshold"])
        self.assertIsNotNone(report["dual_mode_policy"]["pe_threshold"])

    def test_threshold_selection_reproducible(self) -> None:
        df = _make_labeled(days=9, rows_per_day=30)
        cfg = TrainConfig(random_state=11, max_depth=3, n_estimators=80, learning_rate=0.05)
        dcfg = DecisionConfig(threshold_min=0.5, threshold_max=0.8, threshold_step=0.1, cost_per_trade=0.0006)
        r1 = run_calibration_threshold_v2(
            labeled_df=df,
            train_cfg=cfg,
            decision_cfg=dcfg,
            feature_profile="futures_options_only",
            train_days=4,
            valid_days=2,
            test_days=1,
            step_days=1,
            purge_days=1,
            embargo_days=0,
            reliability_bins=6,
        )
        r2 = run_calibration_threshold_v2(
            labeled_df=df,
            train_cfg=cfg,
            decision_cfg=dcfg,
            feature_profile="futures_options_only",
            train_days=4,
            valid_days=2,
            test_days=1,
            step_days=1,
            purge_days=1,
            embargo_days=0,
            reliability_bins=6,
        )
        self.assertEqual(r1["dual_mode_policy"]["ce_threshold"], r2["dual_mode_policy"]["ce_threshold"])
        self.assertEqual(r1["dual_mode_policy"]["pe_threshold"], r2["dual_mode_policy"]["pe_threshold"])

    def test_topk_path_tp_sl_mode(self) -> None:
        df = _make_labeled(days=9, rows_per_day=30)
        report = run_calibration_threshold_v2(
            labeled_df=df,
            train_cfg=TrainConfig(random_state=11, max_depth=3, n_estimators=80, learning_rate=0.05),
            decision_cfg=DecisionConfig(threshold_min=0.5, threshold_max=0.8, threshold_step=0.1, cost_per_trade=0.0006),
            feature_profile="futures_options_only",
            label_target="path_tp_sl",
            selection_mode="topk",
            topk_per_day=10,
            train_days=4,
            valid_days=2,
            test_days=1,
            step_days=1,
            purge_days=1,
            embargo_days=0,
            reliability_bins=6,
        )
        self.assertEqual(report["label_target"], "path_tp_sl")
        self.assertEqual(report["dual_mode_policy"]["selection_mode"], "topk")
        self.assertEqual(report["dual_mode_policy"]["topk_per_day"], 10)
        self.assertIsNone(report["dual_mode_policy"]["ce_threshold"])
        self.assertIsNone(report["dual_mode_policy"]["pe_threshold"])


if __name__ == "__main__":
    unittest.main()
