import unittest

from ml_pipeline.exit_policy import ExitPolicyConfig, parse_exit_policy, validate_exit_policy_dict


class ExitPolicyTests(unittest.TestCase):
    def test_parse_default_policy(self) -> None:
        cfg = parse_exit_policy({})
        self.assertIsInstance(cfg, ExitPolicyConfig)
        self.assertEqual(cfg.version, "v1")
        self.assertEqual(cfg.time_stop_minutes, 3)

    def test_parse_valid_dynamic_policy(self) -> None:
        payload = {
            "time_stop_minutes": 5,
            "stop_loss_pct": 0.10,
            "take_profit_pct": 0.30,
            "enable_trailing_stop": True,
            "trailing_stop_pct": 0.08,
            "move_to_break_even_at_profit_pct": 0.12,
            "allow_hold_extension": True,
            "max_hold_extension_minutes": 2,
            "extension_min_model_prob": 0.72,
            "forced_eod_exit_time": "15:20",
        }
        cfg = parse_exit_policy(payload)
        self.assertTrue(cfg.enable_trailing_stop)
        self.assertTrue(cfg.allow_hold_extension)
        self.assertEqual(cfg.max_hold_extension_minutes, 2)

    def test_invalid_unknown_field(self) -> None:
        result = validate_exit_policy_dict({"unknown": 1})
        self.assertFalse(result.ok)
        self.assertTrue(any("unknown fields" in e for e in result.errors))

    def test_invalid_time_stop_minutes(self) -> None:
        result = validate_exit_policy_dict({"time_stop_minutes": 0})
        self.assertFalse(result.ok)
        self.assertTrue(any("time_stop_minutes" in e for e in result.errors))

    def test_invalid_trailing_without_enable(self) -> None:
        result = validate_exit_policy_dict({"enable_trailing_stop": False, "trailing_stop_pct": 0.05})
        self.assertFalse(result.ok)
        self.assertTrue(any("trailing_stop_pct must be null" in e for e in result.errors))

    def test_invalid_hold_extension_fields_when_disabled(self) -> None:
        result = validate_exit_policy_dict({"allow_hold_extension": False, "max_hold_extension_minutes": 2})
        self.assertFalse(result.ok)
        self.assertTrue(any("max_hold_extension_minutes" in e for e in result.errors))

    def test_invalid_extension_probability_range(self) -> None:
        result = validate_exit_policy_dict(
            {
                "allow_hold_extension": True,
                "max_hold_extension_minutes": 3,
                "extension_min_model_prob": 1.5,
            }
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("extension_min_model_prob" in e for e in result.errors))

    def test_invalid_eod_time_format(self) -> None:
        result = validate_exit_policy_dict({"forced_eod_exit_time": "3:20pm"})
        self.assertFalse(result.ok)
        self.assertTrue(any("HH:MM" in e for e in result.errors))

    def test_invalid_take_profit_not_above_stop(self) -> None:
        result = validate_exit_policy_dict({"stop_loss_pct": 0.12, "take_profit_pct": 0.10})
        self.assertFalse(result.ok)
        self.assertTrue(any("take_profit_pct must be greater than stop_loss_pct" in e for e in result.errors))


if __name__ == "__main__":
    unittest.main()
