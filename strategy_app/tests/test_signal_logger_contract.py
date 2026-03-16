import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from strategy_app.contracts import Direction, SignalType, StrategyVote, TradeSignal
from strategy_app.logging.signal_logger import SignalLogger


class SignalLoggerContractTests(unittest.TestCase):
    def test_logs_engine_aware_vote_and_signal_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            logger = SignalLogger(root)
            logger.set_run_context(
                "run-1",
                {
                    "engine_mode": "ml_pure",
                    "strategy_family_version": "ML_PURE_DUAL_V1",
                    "strategy_profile_id": "ml_pure_dual_v1",
                },
            )
            vote = StrategyVote(
                strategy_name="ML_PURE_DUAL",
                snapshot_id="snap-1",
                timestamp=datetime(2026, 3, 7, 9, 30, tzinfo=timezone.utc),
                trade_date="2026-03-07",
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=0.81,
                reason="ml_pure hold reason=low_edge_conflict",
                decision_reason_code="low_edge_conflict",
                decision_metrics={"ce_prob": 0.62, "pe_prob": 0.60, "edge": 0.02},
            )
            signal = TradeSignal(
                signal_id="sig-1",
                timestamp=datetime(2026, 3, 7, 9, 30, tzinfo=timezone.utc),
                snapshot_id="snap-1",
                signal_type=SignalType.HOLD,
                source="ML_PURE",
                reason="ml_pure_hold:feature_stale",
                decision_reason_code="feature_stale",
                decision_metrics={"confidence": 0.62},
            )

            logger.log_vote(vote)
            logger.log_signal(signal, acted_on=False)

            vote_row = json.loads((root / "votes.jsonl").read_text(encoding="utf-8").strip().splitlines()[0])
            signal_row = json.loads((root / "signals.jsonl").read_text(encoding="utf-8").strip().splitlines()[0])

            self.assertEqual(vote_row["engine_mode"], "ml_pure")
            self.assertEqual(vote_row["strategy_family_version"], "ML_PURE_DUAL_V1")
            self.assertEqual(vote_row["strategy_profile_id"], "ml_pure_dual_v1")
            self.assertEqual(vote_row["decision_reason_code"], "low_edge_conflict")
            self.assertIsInstance(vote_row["decision_metrics"], dict)

            self.assertEqual(signal_row["engine_mode"], "ml_pure")
            self.assertEqual(signal_row["decision_mode"], "ml_dual")
            self.assertEqual(signal_row["decision_reason_code"], "feature_stale")
            self.assertEqual(signal_row["strategy_profile_id"], "ml_pure_dual_v1")
            self.assertEqual(signal_row["acted_on"], False)


if __name__ == "__main__":
    unittest.main()
