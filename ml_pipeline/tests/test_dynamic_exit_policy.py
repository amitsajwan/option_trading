import unittest

from ml_pipeline.dynamic_exit_policy import (
    DynamicExitPolicyConfig,
    simulate_dynamic_exit,
    validate_policy_config,
)


class DynamicExitPolicyTests(unittest.TestCase):
    def test_validate_policy_config(self) -> None:
        cfg = DynamicExitPolicyConfig(
            stop_loss_pct=0.12,
            take_profit_pct=0.24,
            enable_trailing_stop=True,
            trailing_stop_pct=0.08,
            allow_hold_extension=True,
            max_hold_extension_minutes=2,
            extension_min_model_prob=0.7,
            intrabar_tie_break="sl",
        )
        self.assertEqual(validate_policy_config(cfg), [])

    def test_tp_hit(self) -> None:
        cfg = DynamicExitPolicyConfig(
            stop_loss_pct=0.10,
            take_profit_pct=0.20,
            intrabar_tie_break="sl",
        )
        result = simulate_dynamic_exit(
            entry_price=100.0,
            horizon_minutes=3,
            model_prob=0.5,
            config=cfg,
            bars=[
                {"high": 105.0, "low": 98.0, "close": 101.0},
                {"high": 121.0, "low": 100.0, "close": 119.0},
                {"high": 122.0, "low": 118.0, "close": 121.0},
            ],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_reason"], "tp")
        self.assertAlmostEqual(float(result["realized_return"]), 0.20, places=10)

    def test_sl_hit(self) -> None:
        cfg = DynamicExitPolicyConfig(
            stop_loss_pct=0.10,
            take_profit_pct=0.20,
            intrabar_tie_break="sl",
        )
        result = simulate_dynamic_exit(
            entry_price=100.0,
            horizon_minutes=3,
            model_prob=0.5,
            config=cfg,
            bars=[
                {"high": 102.0, "low": 98.0, "close": 99.0},
                {"high": 101.0, "low": 89.0, "close": 90.0},
                {"high": 95.0, "low": 88.0, "close": 89.0},
            ],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_reason"], "sl")
        self.assertAlmostEqual(float(result["realized_return"]), -0.10, places=10)

    def test_same_bar_tie_break(self) -> None:
        bars = [{"high": 121.0, "low": 89.0, "close": 100.0}]
        cfg_sl = DynamicExitPolicyConfig(stop_loss_pct=0.10, take_profit_pct=0.20, intrabar_tie_break="sl")
        result_sl = simulate_dynamic_exit(
            entry_price=100.0,
            bars=bars,
            horizon_minutes=1,
            model_prob=0.5,
            config=cfg_sl,
        )
        self.assertEqual(result_sl["exit_reason"], "sl")

        cfg_tp = DynamicExitPolicyConfig(stop_loss_pct=0.10, take_profit_pct=0.20, intrabar_tie_break="tp")
        result_tp = simulate_dynamic_exit(
            entry_price=100.0,
            bars=bars,
            horizon_minutes=1,
            model_prob=0.5,
            config=cfg_tp,
        )
        self.assertEqual(result_tp["exit_reason"], "tp")

    def test_trailing_stop_transition(self) -> None:
        cfg = DynamicExitPolicyConfig(
            stop_loss_pct=0.12,
            take_profit_pct=0.40,
            enable_trailing_stop=True,
            trailing_stop_pct=0.08,
            move_to_break_even_at_profit_pct=0.05,
            intrabar_tie_break="sl",
        )
        result = simulate_dynamic_exit(
            entry_price=100.0,
            horizon_minutes=4,
            model_prob=0.5,
            config=cfg,
            bars=[
                {"high": 106.0, "low": 99.0, "close": 105.0},
                {"high": 116.0, "low": 107.0, "close": 114.0},
                {"high": 115.0, "low": 106.0, "close": 107.0},
                {"high": 108.0, "low": 100.0, "close": 101.0},
            ],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_reason"], "trail")
        self.assertTrue(result["trailing_active"])

    def test_hold_extension_threshold_behavior(self) -> None:
        cfg = DynamicExitPolicyConfig(
            stop_loss_pct=0.12,
            take_profit_pct=0.30,
            allow_hold_extension=True,
            max_hold_extension_minutes=2,
            extension_min_model_prob=0.7,
            intrabar_tie_break="sl",
        )
        bars = [
            {"high": 104.0, "low": 99.0, "close": 102.0},
            {"high": 106.0, "low": 101.0, "close": 103.0},
            {"high": 107.0, "low": 102.0, "close": 104.0},
            {"high": 108.0, "low": 103.0, "close": 105.0},
            {"high": 109.0, "low": 104.0, "close": 106.0},
        ]
        low_prob = simulate_dynamic_exit(
            entry_price=100.0,
            bars=bars,
            horizon_minutes=3,
            model_prob=0.69,
            config=cfg,
        )
        high_prob = simulate_dynamic_exit(
            entry_price=100.0,
            bars=bars,
            horizon_minutes=3,
            model_prob=0.80,
            config=cfg,
        )
        self.assertFalse(low_prob["hold_extended"])
        self.assertTrue(high_prob["hold_extended"])
        self.assertEqual(low_prob["exit_offset_min"], 2)
        self.assertEqual(high_prob["exit_offset_min"], 4)


if __name__ == "__main__":
    unittest.main()
