import unittest

from strategy_app.engines.pure_ml_inference import infer_action


class PureMLInferenceTests(unittest.TestCase):
    def test_low_edge_conflict_returns_hold_reason(self) -> None:
        action, reason = infer_action(
            ce_prob=0.65,
            pe_prob=0.62,
            ce_threshold=0.60,
            pe_threshold=0.60,
            min_edge=0.15,
        )
        self.assertEqual(action, "HOLD")
        self.assertEqual(reason, "low_edge_conflict")


if __name__ == "__main__":
    unittest.main()
