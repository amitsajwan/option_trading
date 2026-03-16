import unittest

from market_data_dashboard.ux.decision_explainer import build_decision_explainability, explain_reason_code


class DecisionExplainerTests(unittest.TestCase):
    def test_explain_reason_code_known_and_unknown(self) -> None:
        explanation, hint = explain_reason_code("low_edge_conflict")
        self.assertTrue(explanation)
        self.assertTrue(hint)

        fallback_explanation, fallback_hint = explain_reason_code("totally_unknown_reason")
        self.assertIn("not mapped", fallback_explanation.lower())
        self.assertTrue(fallback_hint)

    def test_timeline_prefers_signal_actions_over_vote_when_not_debug(self) -> None:
        signals = [
            {
                "timestamp": "2026-03-07T09:31:00+05:30",
                "signal_id": "sig-1",
                "signal_type": "HOLD",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_dual",
                "decision_reason_code": "low_edge_conflict",
                "decision_metrics": {"edge": 0.01, "confidence": 0.61},
            }
        ]
        votes = [
            {
                "timestamp": "2026-03-07T09:31:00+05:30",
                "snapshot_id": "snap-1",
                "strategy": "EMA_CROSSOVER",
                "signal_type": "ENTRY",
                "policy_allowed": True,
                "engine_mode": "deterministic",
                "decision_mode": "rule_vote",
                "decision_reason_code": "policy_allowed",
            }
        ]
        payload = build_decision_explainability(
            recent_signals=signals,
            recent_votes=votes,
            decision_diagnostics={},
            timeline_limit=25,
            debug_view=False,
        )
        self.assertEqual(len(payload["timeline"]), 1)
        self.assertEqual(payload["timeline"][0]["source_ref"], "signal:sig-1")
        self.assertEqual(payload["latest_decision"]["action"], "HOLD")

    def test_timeline_limit_and_order_in_debug_mode(self) -> None:
        signals = [
            {
                "timestamp": "2026-03-07T09:28:00+05:30",
                "signal_id": "sig-1",
                "signal_type": "ENTRY",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_dual",
                "decision_reason_code": "ce_above_threshold",
                "decision_metrics": {"edge": 0.20},
            }
        ]
        votes = [
            {
                "timestamp": "2026-03-07T09:30:00+05:30",
                "snapshot_id": "snap-2",
                "strategy": "EMA_CROSSOVER",
                "signal_type": "ENTRY",
                "policy_allowed": True,
                "engine_mode": "deterministic",
                "decision_mode": "rule_vote",
                "decision_reason_code": "policy_allowed",
            },
            {
                "timestamp": "2026-03-07T09:29:00+05:30",
                "snapshot_id": "snap-1",
                "strategy": "EMA_CROSSOVER",
                "signal_type": "ENTRY",
                "policy_allowed": False,
                "engine_mode": "deterministic",
                "decision_mode": "rule_vote",
                "decision_reason_code": "policy_block",
            },
        ]
        payload = build_decision_explainability(
            recent_signals=signals,
            recent_votes=votes,
            decision_diagnostics={},
            timeline_limit=2,
            debug_view=True,
        )
        self.assertEqual(len(payload["timeline"]), 2)
        self.assertEqual(payload["timeline"][0]["ts"], "2026-03-07T09:30:00+05:30")
        self.assertEqual(payload["timeline"][1]["ts"], "2026-03-07T09:29:00+05:30")


if __name__ == "__main__":
    unittest.main()
