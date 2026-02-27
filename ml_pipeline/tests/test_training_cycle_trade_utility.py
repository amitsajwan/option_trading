import unittest

import numpy as np
import pandas as pd

from ml_pipeline.training_cycle import (
    LABEL_TARGET_PATH_TP_SL,
    TradingObjectiveConfig,
    run_training_cycle,
)


class TrainingCycleTradeUtilityTests(unittest.TestCase):
    def _build_dataset(self) -> pd.DataFrame:
        rows = []
        rng = np.random.default_rng(42)
        days = pd.date_range("2024-01-01", periods=10, freq="D")
        for d_idx, day in enumerate(days):
            for m in range(6):
                ts = pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=15 + m)
                signal = float((d_idx % 2) * 2 - 1) + float(rng.normal(0.0, 0.2))
                ce_reason = "tp" if signal > 0.0 else "sl"
                pe_reason = "sl" if signal > 0.0 else "tp"
                ce_label = 1 if ce_reason == "tp" else 0
                pe_label = 1 if pe_reason == "tp" else 0
                rows.append(
                    {
                        "timestamp": ts,
                        "trade_date": str(ts.date()),
                        "feat_signal": signal,
                        "feat_noise": float(rng.normal(0.0, 1.0)),
                        "ce_label_valid": 1.0,
                        "pe_label_valid": 1.0,
                        "ce_label": ce_label,
                        "pe_label": pe_label,
                        "ce_path_exit_reason": ce_reason,
                        "pe_path_exit_reason": pe_reason,
                        "ce_forward_return": 0.30 if ce_reason == "tp" else -0.20,
                        "pe_forward_return": 0.30 if pe_reason == "tp" else -0.20,
                        "ce_path_target_valid": 1.0,
                        "pe_path_target_valid": 1.0,
                    }
                )
        return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    def test_trade_utility_objective_runs(self) -> None:
        df = self._build_dataset()
        out = run_training_cycle(
            labeled_df=df,
            feature_profile="all",
            objective="trade_utility",
            label_target=LABEL_TARGET_PATH_TP_SL,
            train_days=4,
            valid_days=1,
            test_days=1,
            step_days=1,
            max_experiments=1,
            random_state=7,
            utility_cfg=TradingObjectiveConfig(
                ce_threshold=0.5,
                pe_threshold=0.5,
                cost_per_trade=0.0,
                min_profit_factor=1.0,
                max_equity_drawdown_pct=1.0,
                min_trades=5,
                take_profit_pct=0.30,
                stop_loss_pct=0.20,
                discard_time_stop=False,
            ),
        )
        report = out["report"]
        best = report["best_experiment"]
        self.assertEqual(report["objective"], "trade_utility")
        self.assertIsNotNone(best["objective_value"])
        utility = best["result"]["trading_utility"]
        self.assertGreaterEqual(int(utility["trades_total"]), 5)
        self.assertTrue(bool(utility["constraints_pass"]))


if __name__ == "__main__":
    unittest.main()
