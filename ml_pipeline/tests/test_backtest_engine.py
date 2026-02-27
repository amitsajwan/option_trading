import unittest

import numpy as np
import pandas as pd

from ml_pipeline.backtest_engine import entry_exit_timestamps, run_backtest
from ml_pipeline.config import TrainConfig
from ml_pipeline.fill_model import FillModelConfig


def _synthetic_labeled(days: int = 6, rows_per_day: int = 24) -> pd.DataFrame:
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
                "ce_tp_price": np.full(rows_per_day, 110.0),
                "ce_sl_price": np.full(rows_per_day, 90.0),
                "ce_tp_hit": np.where(y_ce == 1, 1.0, 0.0),
                "ce_sl_hit": np.where(y_ce == 1, 0.0, 1.0),
                "ce_first_hit": np.where(y_ce == 1, "tp", "sl"),
                "ce_first_hit_offset_min": np.zeros(rows_per_day),
                "ce_path_exit_reason": np.where(y_ce == 1, "tp", "sl"),
                "ce_time_stop_exit": np.zeros(rows_per_day),
                "ce_hold_extension_eligible": np.zeros(rows_per_day),
                "pe_mfe": np.full(rows_per_day, 0.015),
                "pe_mae": np.full(rows_per_day, -0.0045),
                "pe_tp_price": np.full(rows_per_day, 88.0),
                "pe_sl_price": np.full(rows_per_day, 72.0),
                "pe_tp_hit": np.where(y_pe == 1, 1.0, 0.0),
                "pe_sl_hit": np.where(y_pe == 1, 0.0, 1.0),
                "pe_first_hit": np.where(y_pe == 1, "tp", "sl"),
                "pe_first_hit_offset_min": np.zeros(rows_per_day),
                "pe_path_exit_reason": np.where(y_pe == 1, "tp", "sl"),
                "pe_time_stop_exit": np.zeros(rows_per_day),
                "pe_hold_extension_eligible": np.zeros(rows_per_day),
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
        blocks.append(block)
    return pd.concat(blocks, ignore_index=True)


class BacktestEngineTests(unittest.TestCase):
    def test_entry_exit_timing(self) -> None:
        decision = pd.Timestamp("2023-06-15 09:15:00")
        entry, exit_ = entry_exit_timestamps(decision, horizon_minutes=3)
        self.assertEqual(entry, pd.Timestamp("2023-06-15 09:16:00"))
        self.assertEqual(exit_, pd.Timestamp("2023-06-15 09:18:00"))

    def test_cost_application(self) -> None:
        df = _synthetic_labeled(days=5, rows_per_day=20)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=11,
            max_depth=3,
            n_estimators=60,
            learning_rate=0.05,
        )
        cost = 0.001
        trades, report = run_backtest(
            labeled_df=df,
            ce_threshold=0.5,
            pe_threshold=0.5,
            cost_per_trade=cost,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
        )
        self.assertGreater(len(trades), 0)
        net_expected = trades["gross_return"] - cost
        pd.testing.assert_series_equal(
            trades["net_return"].reset_index(drop=True),
            net_expected.reset_index(drop=True),
            check_names=False,
        )
        self.assertAlmostEqual(report["net_return_sum"], float(trades["net_return"].sum()), places=12)

    def test_intrabar_tie_break_deterministic(self) -> None:
        df = _synthetic_labeled(days=5, rows_per_day=20)
        # Force same-bar dual hit path for CE so tie-break decides exit.
        df["ce_path_exit_reason"] = "tp_sl_same_bar"
        df["ce_first_hit_offset_min"] = 0.0
        df["ce_tp_price"] = 110.0
        df["ce_sl_price"] = 90.0

        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=11,
            max_depth=3,
            n_estimators=60,
            learning_rate=0.05,
        )
        trades_sl, _ = run_backtest(
            labeled_df=df,
            ce_threshold=0.0,
            pe_threshold=2.0,
            cost_per_trade=0.0,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
            execution_mode="path_v2",
            intrabar_tie_break="sl",
        )
        self.assertGreater(len(trades_sl), 0)
        self.assertTrue((trades_sl["exit_reason"] == "sl").all())
        self.assertAlmostEqual(float(trades_sl["gross_return"].iloc[0]), -0.10, places=10)

        trades_tp, _ = run_backtest(
            labeled_df=df,
            ce_threshold=0.0,
            pe_threshold=2.0,
            cost_per_trade=0.0,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
            execution_mode="path_v2",
            intrabar_tie_break="tp",
        )
        self.assertGreater(len(trades_tp), 0)
        self.assertTrue((trades_tp["exit_reason"] == "tp").all())
        self.assertAlmostEqual(float(trades_tp["gross_return"].iloc[0]), 0.10, places=10)

    def test_fee_and_slippage_application(self) -> None:
        df = _synthetic_labeled(days=5, rows_per_day=20)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=9,
            max_depth=3,
            n_estimators=50,
            learning_rate=0.05,
        )
        cost = 0.001
        slippage = 0.0007
        trades, report = run_backtest(
            labeled_df=df,
            ce_threshold=0.0,
            pe_threshold=2.0,
            cost_per_trade=cost,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
            execution_mode="path_v2",
            intrabar_tie_break="tp",
            slippage_per_trade=slippage,
        )
        self.assertGreater(len(trades), 0)
        expected = trades["gross_return"] - cost - slippage
        pd.testing.assert_series_equal(
            trades["net_return"].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False,
        )
        self.assertAlmostEqual(report["slippage_per_trade"], slippage, places=12)

    def test_no_lookahead_future_day_mutation(self) -> None:
        base = _synthetic_labeled(days=6, rows_per_day=18)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=7,
            max_depth=3,
            n_estimators=50,
            learning_rate=0.05,
        )

        t1, r1 = run_backtest(
            labeled_df=base,
            ce_threshold=0.5,
            pe_threshold=0.5,
            cost_per_trade=0.0006,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=10,  # force one fold so day-6 is outside split window
        )

        mutated = base.copy()
        last_day = sorted(mutated["trade_date"].unique())[-1]
        mask = mutated["trade_date"] == last_day
        mutated.loc[mask, "feature_a"] = mutated.loc[mask, "feature_a"] * 100.0
        mutated.loc[mask, "feature_b"] = mutated.loc[mask, "feature_b"] * -100.0
        mutated.loc[mask, "ce_label"] = 1 - mutated.loc[mask, "ce_label"]
        mutated.loc[mask, "pe_label"] = 1 - mutated.loc[mask, "pe_label"]

        t2, r2 = run_backtest(
            labeled_df=mutated,
            ce_threshold=0.5,
            pe_threshold=0.5,
            cost_per_trade=0.0006,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=10,
        )

        self.assertEqual(r1["fold_count"], r2["fold_count"])
        cols = ["decision_timestamp", "side", "gross_return", "net_return"]
        pd.testing.assert_frame_equal(
            t1.loc[:, cols].reset_index(drop=True),
            t2.loc[:, cols].reset_index(drop=True),
            check_like=False,
        )

    def test_fill_model_cost_accounting(self) -> None:
        df = _synthetic_labeled(days=5, rows_per_day=20)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=11,
            max_depth=3,
            n_estimators=60,
            learning_rate=0.05,
        )
        fill_cfg = FillModelConfig(
            model="spread_fraction",
            constant_slippage=0.0,
            spread_fraction=0.5,
            volume_impact_coeff=0.0,
            min_slippage=0.0,
            max_slippage=0.02,
        )
        trades, report = run_backtest(
            labeled_df=df,
            ce_threshold=0.0,
            pe_threshold=2.0,
            cost_per_trade=0.001,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
            execution_mode="path_v2",
            intrabar_tie_break="tp",
            slippage_per_trade=0.0002,
            fill_model_config=fill_cfg,
        )
        self.assertGreater(len(trades), 0)
        self.assertTrue((trades["slippage_model_component"] >= 0.0).all())
        expected = trades["gross_return"] - 0.001 - 0.0002 - trades["slippage_model_component"]
        pd.testing.assert_series_equal(
            trades["net_return"].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False,
        )
        self.assertAlmostEqual(report["net_return_sum"], float(trades["net_return"].sum()), places=12)


if __name__ == "__main__":
    unittest.main()
