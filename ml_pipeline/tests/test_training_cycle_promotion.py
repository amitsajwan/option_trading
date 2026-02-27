import unittest

import numpy as np
import pandas as pd

from ml_pipeline.training_cycle import LABEL_TARGET_PATH_TP_SL, TradingObjectiveConfig, run_training_cycle


def _dataset_with_time_stops(days: int = 8, rows_per_day: int = 8) -> pd.DataFrame:
    rows = []
    start = pd.Timestamp("2024-01-01")
    for d in range(days):
        day = start + pd.Timedelta(days=d)
        for m in range(rows_per_day):
            ts = day + pd.Timedelta(hours=9, minutes=15 + m)
            signal = float(np.sin((d + 1) * 0.3 + m * 0.2))
            if m % 3 == 0:
                ce_reason = "time_stop"
                pe_reason = "time_stop"
                ce_ret = 0.01
                pe_ret = -0.005
            else:
                ce_reason = "tp" if signal > 0 else "sl"
                pe_reason = "sl" if signal > 0 else "tp"
                ce_ret = 0.30 if ce_reason == "tp" else -0.20
                pe_ret = 0.30 if pe_reason == "tp" else -0.20
            rows.append(
                {
                    "timestamp": ts,
                    "trade_date": str(ts.date()),
                    "ret_1m": signal,
                    "ret_5m": signal * 0.7,
                    "rsi_14": 50.0 + signal * 10.0,
                    "atr_ratio": 0.002 + abs(signal) * 0.001,
                    "vwap_distance": signal * 0.001,
                    "distance_from_day_high": signal * 0.0008,
                    "distance_from_day_low": -signal * 0.0008,
                    "atm_call_return_1m": signal * 0.02,
                    "atm_put_return_1m": -signal * 0.02,
                    "atm_oi_change_1m": signal * 100.0,
                    "ce_pe_oi_diff": signal * 500.0,
                    "ce_pe_volume_diff": signal * 300.0,
                    "pcr_oi": 1.0 + signal * 0.05,
                    "minute_of_day": int(ts.hour * 60 + ts.minute),
                    "day_of_week": int(ts.dayofweek),
                    "opening_range_breakout_up": float(signal > 0.4),
                    "opening_range_breakout_down": float(signal < -0.4),
                    "ce_label_valid": 1.0,
                    "pe_label_valid": 1.0,
                    "ce_label": float(ce_reason == "tp"),
                    "pe_label": float(pe_reason == "tp"),
                    "ce_path_exit_reason": ce_reason,
                    "pe_path_exit_reason": pe_reason,
                    "ce_forward_return": ce_ret,
                    "pe_forward_return": pe_ret,
                    "ce_path_target_valid": float(ce_reason in {"tp", "sl", "tp_sl_same_bar"}),
                    "pe_path_target_valid": float(pe_reason in {"tp", "sl", "tp_sl_same_bar"}),
                }
            )
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


class TrainingCyclePromotionTests(unittest.TestCase):
    def test_no_promotable_model_flag_when_constraints_fail(self) -> None:
        df = _dataset_with_time_stops(days=8, rows_per_day=8)
        out = run_training_cycle(
            labeled_df=df,
            feature_profile="core_v1",
            objective="trade_utility",
            label_target=LABEL_TARGET_PATH_TP_SL,
            train_days=4,
            valid_days=1,
            test_days=1,
            step_days=1,
            max_experiments=2,
            random_state=11,
            utility_cfg=TradingObjectiveConfig(
                ce_threshold=0.6,
                pe_threshold=0.6,
                cost_per_trade=0.02,
                min_profit_factor=3.0,
                max_equity_drawdown_pct=0.01,
                min_trades=1000,
                take_profit_pct=0.30,
                stop_loss_pct=0.20,
                discard_time_stop=False,
            ),
        )
        promotion = out["report"]["promotion"]
        self.assertTrue(bool(promotion["no_promotable_model"]))
        best = out["report"]["best_experiment"]
        self.assertTrue(bool(best.get("selected_by_fallback", False)))
        self.assertIn("fallback_objective_value", best)


if __name__ == "__main__":
    unittest.main()

