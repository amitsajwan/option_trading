import unittest

import numpy as np
import pandas as pd

from ml_pipeline.config import TrainConfig
from ml_pipeline.strategy_comparison_v2 import run_strategy_comparison_v2


def _synthetic_labeled(days: int = 6, rows_per_day: int = 18) -> pd.DataFrame:
    blocks = []
    start = pd.Timestamp("2023-01-02 09:15:00")
    for d in range(days):
        ts = pd.date_range(start + pd.Timedelta(days=d), periods=rows_per_day, freq="min")
        idx = np.arange(rows_per_day)
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
                "feature_a": np.sin(idx / 3.0 + d),
                "feature_b": np.cos(idx / 4.0 + d * 0.5),
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
                "ce_tp_price": np.full(rows_per_day, 110.0),
                "ce_sl_price": np.full(rows_per_day, 90.0),
                "ce_first_hit_offset_min": np.zeros(rows_per_day),
                "ce_path_exit_reason": np.where(y_ce == 1, "tp", "time_stop"),
                "ce_tp_hit": np.where(y_ce == 1, 1.0, 0.0),
                "ce_sl_hit": np.zeros(rows_per_day),
                "ce_time_stop_exit": np.where(y_ce == 1, 0.0, 1.0),
                "ce_hold_extension_eligible": np.zeros(rows_per_day),
                "pe_tp_price": np.full(rows_per_day, 88.0),
                "pe_sl_price": np.full(rows_per_day, 72.0),
                "pe_first_hit_offset_min": np.zeros(rows_per_day),
                "pe_path_exit_reason": np.where(y_pe == 1, "tp", "time_stop"),
                "pe_tp_hit": np.where(y_pe == 1, 1.0, 0.0),
                "pe_sl_hit": np.zeros(rows_per_day),
                "pe_time_stop_exit": np.where(y_pe == 1, 0.0, 1.0),
                "pe_hold_extension_eligible": np.zeros(rows_per_day),
                "label_horizon_minutes": np.full(rows_per_day, 3),
                "label_return_threshold": np.full(rows_per_day, 0.002),
                "best_side_label": np.where(y_ce >= y_pe, 1, -1),
            }
        )
        blocks.append(block)
    return pd.concat(blocks, ignore_index=True)


class StrategyComparisonV2Tests(unittest.TestCase):
    def test_consistency_across_profiles(self) -> None:
        df = _synthetic_labeled(days=7, rows_per_day=20)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=42,
            max_depth=3,
            n_estimators=60,
            learning_rate=0.05,
        )
        profiles = [
            {
                "name": "fixed_horizon",
                "execution_mode": "fixed_horizon",
                "intrabar_tie_break": "sl",
                "slippage_per_trade": 0.0,
                "forced_eod_exit_time": "15:24",
                "cost_per_trade": 0.0006,
            },
            {
                "name": "path_v2",
                "execution_mode": "path_v2",
                "intrabar_tie_break": "sl",
                "slippage_per_trade": 0.0002,
                "forced_eod_exit_time": "15:24",
                "cost_per_trade": 0.0006,
            },
        ]
        report = run_strategy_comparison_v2(
            labeled_df=df,
            ce_threshold=0.5,
            pe_threshold=0.5,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
            profiles=profiles,
        )
        self.assertEqual(report["consistency_check"]["test_rows_total"], report["results"][0]["summary"]["test_rows_total"])
        self.assertEqual(report["consistency_check"]["fold_count"], report["results"][0]["summary"]["fold_count"])
        self.assertEqual(len(report["results"]), 2)

    def test_deterministic_repeated_run(self) -> None:
        df = _synthetic_labeled(days=6, rows_per_day=18)
        cfg = TrainConfig(
            train_ratio=0.7,
            valid_ratio=0.15,
            random_state=7,
            max_depth=3,
            n_estimators=50,
            learning_rate=0.05,
        )
        profiles = [
            {
                "name": "fixed_horizon",
                "execution_mode": "fixed_horizon",
                "intrabar_tie_break": "sl",
                "slippage_per_trade": 0.0,
                "forced_eod_exit_time": "15:24",
                "cost_per_trade": 0.0006,
            },
            {
                "name": "path_v2",
                "execution_mode": "path_v2",
                "intrabar_tie_break": "tp",
                "slippage_per_trade": 0.0002,
                "forced_eod_exit_time": "15:24",
                "cost_per_trade": 0.0006,
            },
        ]
        a = run_strategy_comparison_v2(
            labeled_df=df,
            ce_threshold=0.5,
            pe_threshold=0.5,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
            profiles=profiles,
        )
        b = run_strategy_comparison_v2(
            labeled_df=df,
            ce_threshold=0.5,
            pe_threshold=0.5,
            train_config=cfg,
            train_days=3,
            valid_days=1,
            test_days=1,
            step_days=1,
            profiles=profiles,
        )
        self.assertEqual(a["best_profile"], b["best_profile"])
        self.assertEqual(a["best_summary"]["net_return_sum"], b["best_summary"]["net_return_sum"])


if __name__ == "__main__":
    unittest.main()
