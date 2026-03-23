import unittest
from datetime import datetime

from contracts_app import IST_ZONE
from strategy_app.contracts import Direction, SignalType, StrategyVote
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.engines.entry_policy import EntryPolicyDecision
from strategy_app.engines.regime import Regime, RegimeSignal
from strategy_app.engines.snapshot_accessor import SnapshotAccessor


def _snapshot() -> SnapshotAccessor:
    return SnapshotAccessor(
        {
            "snapshot_id": "snap-boost",
            "session_context": {
                "snapshot_id": "snap-boost",
                "timestamp": "2026-03-05T10:30:00+05:30",
                "date": "2026-03-05",
                "session_phase": "ACTIVE",
                "minutes_since_open": 75,
                "days_to_expiry": 1,
            },
            "chain_aggregates": {"atm_strike": 50000},
            "atm_options": {"atm_ce_close": 100.0, "atm_pe_close": 100.0},
            "strikes": [{"strike": 50000, "ce_ltp": 100.0, "pe_ltp": 100.0}],
        }
    )


class PostHaltResumeBoostTests(unittest.TestCase):
    def test_boost_applies_once_on_first_entry(self) -> None:
        engine = DeterministicRuleEngine()
        engine._risk.context.post_halt_resume_boost_available = True  # type: ignore[attr-defined]
        snap = _snapshot()
        vote = StrategyVote(
            strategy_name="ORB",
            snapshot_id="snap-boost",
            timestamp=datetime(2026, 3, 5, 10, 30, tzinfo=IST_ZONE),
            trade_date="2026-03-05",
            signal_type=SignalType.ENTRY,
            direction=Direction.CE,
            confidence=0.8,
            reason="test",
            proposed_strike=50000,
            proposed_entry_premium=100.0,
        )
        regime = RegimeSignal(regime=Regime.TRENDING, confidence=0.8, reason="test", evidence={})
        policy = EntryPolicyDecision.allow("ok", score=0.9, checks={})

        signal = engine._build_entry_signal(vote, snap, engine._risk.context, [vote], regime, policy)  # type: ignore[attr-defined]
        self.assertIsNotNone(signal)
        self.assertAlmostEqual(float(signal.confidence or 0.0), 0.608, places=3)
        self.assertFalse(engine._risk.post_halt_resume_boost_available)  # type: ignore[attr-defined]
        self.assertTrue(bool(vote.raw_signals.get("_post_halt_resume_boost_applied")))


if __name__ == "__main__":
    unittest.main()
