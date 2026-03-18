import unittest

from contracts_app.strategy_decision_contract import (
    extract_reason_code_from_text,
    merge_decision_metrics,
    normalize_decision_mode,
    normalize_engine_mode,
    normalize_reason_code,
    parse_metric_token,
)


class StrategyDecisionContractTests(unittest.TestCase):
    def test_normalization(self) -> None:
        self.assertEqual(normalize_engine_mode("ML_PURE"), "ml_pure")
        self.assertEqual(normalize_decision_mode("ML_GATE"), "ml_gate")
        self.assertEqual(normalize_decision_mode("ML_STAGED"), "ml_staged")
        self.assertEqual(normalize_reason_code("warmup_blocked"), "entry_warmup_block")
        self.assertIsNone(normalize_engine_mode("unknown"))

    def test_reason_extraction(self) -> None:
        self.assertEqual(extract_reason_code_from_text("ml_pure_hold:feature_stale"), "feature_stale")
        self.assertEqual(extract_reason_code_from_text("foo reason=below_threshold"), "below_threshold")
        self.assertEqual(extract_reason_code_from_text("low_edge_conflict detected"), "low_edge_conflict")

    def test_metric_token_and_merge(self) -> None:
        self.assertAlmostEqual(float(parse_metric_token("score=0.61", "score")), 0.61, places=6)
        self.assertAlmostEqual(float(parse_metric_token("ce_prob=0.58", "ce_prob")), 0.58, places=6)
        merged = merge_decision_metrics({"a": 1, "b": "x"}, {"a": 2.5, "c": "3.0"})
        self.assertEqual(merged, {"a": 2.5, "c": 3.0})


if __name__ == "__main__":
    unittest.main()
