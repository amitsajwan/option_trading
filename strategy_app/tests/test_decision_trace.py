import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from strategy_app.contracts import BaseStrategy, Direction, RiskContext, SignalType, StrategyVote
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.engines.entry_policy import EntryPolicyDecision
from strategy_app.engines.regime import Regime, RegimeSignal
from strategy_app.logging.signal_logger import SignalLogger


def _snapshot(snapshot_id: str, ts: str) -> dict:
    return {
        "session_context": {
            "snapshot_id": snapshot_id,
            "timestamp": ts,
            "date": ts[:10],
            "session_phase": "ACTIVE",
            "minutes_since_open": 60,
        },
        "chain_aggregates": {"atm_strike": 50000},
        "atm_options": {
            "atm_ce_close": 100.0,
            "atm_pe_close": 100.0,
            "atm_ce_oi": 100000.0,
            "atm_pe_oi": 100000.0,
            "atm_ce_volume": 100000.0,
            "atm_pe_volume": 100000.0,
        },
    }


class _StaticEntryStrategy(BaseStrategy):
    def __init__(self, name: str, *, confidence: float = 0.8) -> None:
        self._name = name
        self._confidence = confidence

    @property
    def name(self) -> str:
        return self._name

    def evaluate(self, snapshot, position, risk):
        del snapshot, position, risk
        return StrategyVote(
            strategy_name=self._name,
            snapshot_id="snap-1",
            timestamp=datetime(2026, 3, 7, 9, 30, tzinfo=timezone.utc),
            trade_date="2026-03-07",
            signal_type=SignalType.ENTRY,
            direction=Direction.CE,
            confidence=self._confidence,
            reason=f"{self._name.lower()} vote",
            proposed_strike=50000,
            proposed_entry_premium=100.0,
        )


class _AllowPolicy:
    def evaluate(self, snap, vote, regime, risk):
        del snap, vote, regime, risk
        return EntryPolicyDecision.allow("allowed score=0.80", score=0.80, checks={"volume": "PASS"}, adjustments={})


class _BlockPolicy:
    def evaluate(self, snap, vote, regime, risk):
        del snap, vote, regime, risk
        return EntryPolicyDecision.block("volume: BLOCK:vol_ratio=0.50", {"volume": "BLOCK:vol_ratio=0.50"})


class DecisionTraceTests(unittest.TestCase):
    def test_deterministic_engine_logs_entry_taken_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SignalLogger(Path(tmpdir))
            engine = DeterministicRuleEngine(signal_logger=logger, entry_policy=_AllowPolicy())
            engine.on_session_start(date(2026, 3, 7))
            engine._regime.classify = lambda snap: RegimeSignal(regime=Regime.TRENDING, confidence=0.9, reason="test", evidence={})  # type: ignore[method-assign]
            engine._router.get_strategies = lambda regime, position: [_StaticEntryStrategy("ORB")]  # type: ignore[method-assign]
            engine._router.regime_allows_entry = lambda regime: True  # type: ignore[method-assign]

            signal = engine.evaluate(_snapshot("snap-1", "2026-03-07T09:30:00+00:00"))

            self.assertIsNotNone(signal)
            rows = [json.loads(line) for line in (Path(tmpdir) / "decision_traces.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["final_outcome"], "entry_taken")
            self.assertEqual(rows[-1]["engine_mode"], "deterministic")
            self.assertEqual(rows[-1]["selected_candidate_id"], rows[-1]["candidates"][0]["candidate_id"])

    def test_deterministic_engine_logs_blocked_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SignalLogger(Path(tmpdir))
            engine = DeterministicRuleEngine(signal_logger=logger, entry_policy=_BlockPolicy())
            engine.on_session_start(date(2026, 3, 7))
            engine._regime.classify = lambda snap: RegimeSignal(regime=Regime.TRENDING, confidence=0.9, reason="test", evidence={})  # type: ignore[method-assign]
            engine._router.get_strategies = lambda regime, position: [_StaticEntryStrategy("ORB")]  # type: ignore[method-assign]
            engine._router.regime_allows_entry = lambda regime: True  # type: ignore[method-assign]

            signal = engine.evaluate(_snapshot("snap-2", "2026-03-07T09:31:00+00:00"))

            self.assertIsNone(signal)
            rows = [json.loads(line) for line in (Path(tmpdir) / "decision_traces.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["final_outcome"], "blocked")
            self.assertEqual(rows[-1]["primary_blocker_gate"], "policy_gate")


if __name__ == "__main__":
    unittest.main()
