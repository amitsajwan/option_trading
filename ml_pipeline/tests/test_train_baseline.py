import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from ml_pipeline.config import TrainConfig
from ml_pipeline.train_baseline import (
    FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    FEATURE_PROFILE_CORE_V1,
    FEATURE_PROFILE_CORE_V2,
    compute_metrics,
    save_training_artifacts,
    select_feature_columns,
    train_baseline_models,
)


def _synthetic_labeled(rows: int = 240) -> pd.DataFrame:
    ts = pd.date_range("2023-06-15 09:15:00", periods=rows, freq="min")
    x1 = np.sin(np.linspace(0, 12, rows))
    x2 = np.cos(np.linspace(0, 8, rows))
    y_ce = (x1 + (0.2 * x2) > 0).astype(int)
    y_pe = (x2 - (0.1 * x1) > 0).astype(int)
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "trade_date": [str(t.date()) for t in ts],
            "source_day": [str(ts[0].date())] * rows,
            "feature_a": x1,
            "feature_b": x2,
            "feature_c": np.linspace(-1.0, 1.0, rows),
            "minute_of_day": ts.hour * 60 + ts.minute,
            "ce_label_valid": np.ones(rows),
            "pe_label_valid": np.ones(rows),
            "ce_label": y_ce,
            "pe_label": y_pe,
            "ce_forward_return": np.where(y_ce == 1, 0.01, -0.005),
            "pe_forward_return": np.where(y_pe == 1, 0.012, -0.006),
            "ce_entry_price": np.full(rows, 100.0),
            "ce_exit_price": np.where(y_ce == 1, 101.0, 99.5),
            "pe_entry_price": np.full(rows, 80.0),
            "pe_exit_price": np.where(y_pe == 1, 81.0, 79.5),
            "ce_mfe": np.full(rows, 0.015),
            "ce_mae": np.full(rows, -0.004),
            "pe_mfe": np.full(rows, 0.017),
            "pe_mae": np.full(rows, -0.005),
            "best_side_label": np.where(y_ce >= y_pe, 1, -1),
            "label_horizon_minutes": np.full(rows, 3),
            "label_return_threshold": np.full(rows, 0.002),
            "fut_symbol": ["BANKNIFTY-I"] * rows,
            "expiry_code": ["15JUN23"] * rows,
            "ce_symbol": ["BANKNIFTY15JUN2344000CE"] * rows,
            "pe_symbol": ["BANKNIFTY15JUN2344000PE"] * rows,
        }
    )
    return df


class TrainBaselineTests(unittest.TestCase):
    def test_compute_metrics_range(self) -> None:
        y_true = np.array([0, 0, 1, 1, 1, 0])
        y_prob = np.array([0.1, 0.2, 0.8, 0.7, 0.6, 0.4])
        metrics = compute_metrics(y_true, y_prob, threshold=0.5)
        self.assertGreaterEqual(metrics["accuracy"], 0.0)
        self.assertLessEqual(metrics["accuracy"], 1.0)
        self.assertGreaterEqual(metrics["precision"], 0.0)
        self.assertLessEqual(metrics["precision"], 1.0)
        self.assertIsNotNone(metrics["roc_auc"])
        self.assertIsNotNone(metrics["pr_auc"])

    def test_select_feature_columns_excludes_labels(self) -> None:
        df = _synthetic_labeled(40)
        df["spot_close"] = np.linspace(100.0, 102.0, len(df))
        df["basis"] = np.linspace(1.0, 2.0, len(df))
        df["depth_total_bid_qty"] = np.linspace(1000.0, 1200.0, len(df))
        df["ret_1m"] = np.linspace(-0.001, 0.001, len(df))
        df["ret_5m"] = np.linspace(-0.002, 0.002, len(df))
        df["vwap_distance"] = np.linspace(-0.003, 0.003, len(df))
        df["distance_from_day_high"] = np.linspace(-0.004, 0.0, len(df))
        df["distance_from_day_low"] = np.linspace(0.0, 0.004, len(df))
        df["opening_range_breakout_up"] = (np.arange(len(df)) % 7 == 0).astype(float)
        df["opening_range_breakout_down"] = (np.arange(len(df)) % 11 == 0).astype(float)
        df["rsi_14"] = np.linspace(35.0, 65.0, len(df))
        df["ema_9_21_spread"] = np.linspace(-20.0, 20.0, len(df))
        df["atr_ratio"] = np.linspace(0.001, 0.005, len(df))
        df["atm_call_return_1m"] = np.linspace(-0.01, 0.01, len(df))
        df["atm_put_return_1m"] = np.linspace(0.01, -0.01, len(df))
        df["atm_oi_change_1m"] = np.linspace(-200.0, 200.0, len(df))
        df["pcr_oi"] = np.linspace(0.8, 1.2, len(df))
        df["ce_pe_oi_diff"] = np.linspace(-5000.0, 5000.0, len(df))
        df["ce_pe_volume_diff"] = np.linspace(-3000.0, 3000.0, len(df))
        df["dte_days"] = np.linspace(0.0, 5.0, len(df))
        df["is_expiry_day"] = (df["dte_days"] == 0.0).astype(float)
        df["is_near_expiry"] = (df["dte_days"] <= 1.0).astype(float)
        df["vix_prev_close"] = np.linspace(12.0, 18.0, len(df))
        df["vix_prev_close_change_1d"] = np.linspace(-0.02, 0.03, len(df))
        df["vix_prev_close_zscore_20d"] = np.linspace(-1.0, 1.0, len(df))
        df["is_high_vix_day"] = (df["vix_prev_close"] >= 20.0).astype(float)
        cols = select_feature_columns(df)
        self.assertIn("feature_a", cols)
        self.assertIn("feature_b", cols)
        self.assertNotIn("ce_label", cols)
        self.assertNotIn("pe_label", cols)
        self.assertNotIn("ce_forward_return", cols)
        fo_cols = select_feature_columns(df, feature_profile=FEATURE_PROFILE_FUTURES_OPTIONS_ONLY)
        self.assertNotIn("spot_close", fo_cols)
        self.assertNotIn("basis", fo_cols)
        self.assertNotIn("depth_total_bid_qty", fo_cols)
        core_cols = select_feature_columns(df, feature_profile=FEATURE_PROFILE_CORE_V1)
        self.assertIn("ema_9_21_spread", core_cols)
        self.assertIn("rsi_14", core_cols)
        self.assertNotIn("feature_a", core_cols)
        self.assertNotIn("spot_close", core_cols)
        self.assertNotIn("depth_total_bid_qty", core_cols)
        core_v2_cols = select_feature_columns(df, feature_profile=FEATURE_PROFILE_CORE_V2)
        self.assertIn("dte_days", core_v2_cols)
        self.assertIn("vix_prev_close", core_v2_cols)
        self.assertIn("vix_prev_close_zscore_20d", core_v2_cols)

    def test_train_deterministic_smoke(self) -> None:
        df = _synthetic_labeled(260)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=11,
            max_depth=3,
            n_estimators=80,
            learning_rate=0.05,
        )
        report1, models1 = train_baseline_models(df, cfg)
        report2, models2 = train_baseline_models(df, cfg)

        feat_cols = report1["feature_columns"]
        x = df.loc[:49, feat_cols]
        p1 = models1["ce"].predict_proba(x)[:, 1]
        p2 = models2["ce"].predict_proba(x)[:, 1]
        np.testing.assert_allclose(p1, p2, rtol=1e-9, atol=1e-9)
        self.assertEqual(report1["feature_count"], report2["feature_count"])

        with tempfile.TemporaryDirectory() as tmp:
            model_out = Path(tmp) / "model.joblib"
            report_out = Path(tmp) / "report.json"
            save_training_artifacts(report1, models1, model_out=model_out, report_out=report_out)
            self.assertTrue(model_out.exists())
            self.assertTrue(report_out.exists())


if __name__ == "__main__":
    unittest.main()
