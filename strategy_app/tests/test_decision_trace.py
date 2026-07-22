import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from strategy_app.contracts import BaseStrategy, Direction, RiskContext, SignalType, StrategyVote
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.policy.entry_policy import EntryPolicyDecision
from strategy_app.market.regime import Regime, RegimeSignal
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

            signal = engine.evaluate(_snapshot("snap-1", "2026-03-07T09:00:00+00:00"))

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

            signal = engine.evaluate(_snapshot("snap-1", "2026-03-07T09:01:00+00:00"))

            self.assertIsNone(signal)
            rows = [json.loads(line) for line in (Path(tmpdir) / "decision_traces.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["final_outcome"], "blocked")
            self.assertEqual(rows[-1]["primary_blocker_gate"], "policy_gate")

    def test_deterministic_engine_emits_decision_summary_on_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SignalLogger(Path(tmpdir))
            engine = DeterministicRuleEngine(signal_logger=logger, entry_policy=_AllowPolicy())
            engine.on_session_start(date(2026, 3, 7))
            engine._regime.classify = lambda snap: RegimeSignal(regime=Regime.TRENDING, confidence=0.9, reason="test", evidence={})  # type: ignore[method-assign]
            engine._router.get_strategies = lambda regime, position: [_StaticEntryStrategy("ORB")]  # type: ignore[method-assign]
            engine._router.regime_allows_entry = lambda regime: True  # type: ignore[method-assign]

            engine.evaluate(_snapshot("snap-1", "2026-03-07T09:00:00+00:00"))

            rows = [json.loads(line) for line in (Path(tmpdir) / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            summary = rows[-1]
            self.assertEqual(summary["snapshot_id"], "snap-1")
            self.assertEqual(summary["action"], "entry_taken")
            self.assertIsNone(summary["blocking_gate"])
            self.assertEqual(summary["engine_mode"], "deterministic")
            self.assertFalse(summary["engine_state"]["has_position"])
            # The single tick line carries input, votes, and output — no second grep needed.
            self.assertEqual(summary["input"]["regime"], "TRENDING")
            self.assertEqual(summary["output"]["signal_type"], "ENTRY")
            self.assertEqual(summary["output"]["direction"], "CE")
            self.assertTrue(any(v["strategy"] == "ORB" for v in summary["votes"]))

    def test_deterministic_engine_summary_records_blocking_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SignalLogger(Path(tmpdir))
            engine = DeterministicRuleEngine(signal_logger=logger, entry_policy=_BlockPolicy())
            engine.on_session_start(date(2026, 3, 7))
            engine._regime.classify = lambda snap: RegimeSignal(regime=Regime.TRENDING, confidence=0.9, reason="test", evidence={})  # type: ignore[method-assign]
            engine._router.get_strategies = lambda regime, position: [_StaticEntryStrategy("ORB")]  # type: ignore[method-assign]
            engine._router.regime_allows_entry = lambda regime: True  # type: ignore[method-assign]

            engine.evaluate(_snapshot("snap-1", "2026-03-07T09:01:00+00:00"))

            rows = [json.loads(line) for line in (Path(tmpdir) / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
            summary = rows[-1]
            self.assertEqual(summary["action"], "blocked")
            self.assertEqual(summary["blocking_gate"], "policy_gate")
            self.assertNotIn("output", summary)

    def test_deterministic_engine_soft_close_blocks_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SignalLogger(Path(tmpdir))
            engine = DeterministicRuleEngine(signal_logger=logger, entry_policy=_AllowPolicy())
            engine.on_session_start(date(2026, 3, 7))
            engine._regime.classify = lambda snap: RegimeSignal(regime=Regime.TRENDING, confidence=0.9, reason="test", evidence={})  # type: ignore[method-assign]
            engine._router.get_strategies = lambda regime, position: [_StaticEntryStrategy("ORB")]  # type: ignore[method-assign]
            engine._router.regime_allows_entry = lambda regime: True  # type: ignore[method-assign]

            # 09:30 UTC == 15:00 IST → soft close gate active
            signal = engine.evaluate(_snapshot("snap-late", "2026-03-07T09:30:00+00:00"))

            self.assertIsNone(signal)
            rows = [json.loads(line) for line in (Path(tmpdir) / "decision_traces.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["final_outcome"], "blocked")
            self.assertEqual(rows[-1]["primary_blocker_gate"], "soft_close_no_entry")


class ShadowDirectionTraceTests(unittest.TestCase):
    """shadow_direction / composite_depth_shadow (2026-07-22).

    shadow_direction fixes a real, previously-live bug: shadow_dir/basis
    (strings) were only ever written into summary_metrics, which runs
    everything through compact_metrics()'s float-only sanitizer -- both were
    silently dropped on every trace ever written, confirmed against real
    live traces before this fix. Same root-cause family as the
    direction_mode/direction_sources bug fixed 2026-07-21
    (contracts_app.merge_decision_metrics).

    composite_depth_shadow is new: composite scoring (incl. live depth,
    weight 1.1) evaluated unconditionally every bar for BANKNIFTY, which
    runs direction_ml -- a trained model with zero depth features -- as its
    REAL decision mechanism. Purely observational, must never affect the
    real signal/gating outcome.
    """

    def test_shadow_direction_persists_in_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SignalLogger(Path(tmpdir))
            engine = DeterministicRuleEngine(signal_logger=logger, entry_policy=_BlockPolicy())
            engine.on_session_start(date(2026, 3, 7))
            engine._regime.classify = lambda snap: RegimeSignal(regime=Regime.TRENDING, confidence=0.9, reason="test", evidence={})  # type: ignore[method-assign]
            engine._router.get_strategies = lambda regime, position: [_StaticEntryStrategy("ORB")]  # type: ignore[method-assign]
            engine._router.regime_allows_entry = lambda regime: True  # type: ignore[method-assign]

            engine.evaluate(_snapshot("snap-1", "2026-03-07T09:01:00+00:00"))

            rows = [json.loads(line) for line in (Path(tmpdir) / "decision_traces.jsonl").read_text(encoding="utf-8").splitlines()]
            shadow = rows[-1]["summary_metrics"]  # legacy numeric-only field, unchanged
            self.assertIn("shadow_score", shadow)
            shadow_direction = rows[-1]["shadow_direction"]  # new: strings survive here
            self.assertIn("dir", shadow_direction)
            self.assertIn("basis", shadow_direction)
            self.assertIn("score", shadow_direction)
            self.assertIn(shadow_direction["dir"], ("CE", "PE"))

    def test_composite_depth_shadow_present_when_scoring_succeeds(self) -> None:
        from strategy_app.ml.entry_direction_resolver import EntryDirectionResult
        from strategy_app.contracts import Direction as _Dir

        fake_result = EntryDirectionResult(
            direction=_Dir.CE, source="composite(depth_ce:bid_dom->CE)",
            ce_score=1.2, pe_score=0.1, margin=1.1,
            sources={"depth_ce:bid_dom->CE": 1.1, "momentum_5m:CE": 1.0},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SignalLogger(Path(tmpdir))
            engine = DeterministicRuleEngine(signal_logger=logger, entry_policy=_BlockPolicy())
            engine.on_session_start(date(2026, 3, 7))
            engine._regime.classify = lambda snap: RegimeSignal(regime=Regime.TRENDING, confidence=0.9, reason="test", evidence={})  # type: ignore[method-assign]
            engine._router.get_strategies = lambda regime, position: [_StaticEntryStrategy("ORB")]  # type: ignore[method-assign]
            engine._router.regime_allows_entry = lambda regime: True  # type: ignore[method-assign]

            with patch(
                "strategy_app.ml.entry_direction_resolver.resolve_entry_direction_composite",
                return_value=fake_result,
            ):
                engine.evaluate(_snapshot("snap-1", "2026-03-07T09:01:00+00:00"))

            rows = [json.loads(line) for line in (Path(tmpdir) / "decision_traces.jsonl").read_text(encoding="utf-8").splitlines()]
            shadow = rows[-1]["composite_depth_shadow"]
            self.assertEqual(shadow["dir"], "CE")
            self.assertIn("depth_ce:bid_dom->CE", shadow["source"])
            self.assertAlmostEqual(shadow["margin"], 1.1, places=4)
            self.assertIn("depth_ce:bid_dom->CE", shadow["sources"])

    def test_composite_depth_shadow_failure_does_not_affect_real_entry(self) -> None:
        """The whole point of 'shadow': if composite scoring blows up, the
        REAL entry_taken decision (driven by the mocked strategy vote, not
        by composite at all) must be completely unaffected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SignalLogger(Path(tmpdir))
            engine = DeterministicRuleEngine(signal_logger=logger, entry_policy=_AllowPolicy())
            engine.on_session_start(date(2026, 3, 7))
            engine._regime.classify = lambda snap: RegimeSignal(regime=Regime.TRENDING, confidence=0.9, reason="test", evidence={})  # type: ignore[method-assign]
            engine._router.get_strategies = lambda regime, position: [_StaticEntryStrategy("ORB")]  # type: ignore[method-assign]
            engine._router.regime_allows_entry = lambda regime: True  # type: ignore[method-assign]

            with patch(
                "strategy_app.ml.entry_direction_resolver.resolve_entry_direction_composite",
                side_effect=RuntimeError("boom"),
            ):
                signal = engine.evaluate(_snapshot("snap-1", "2026-03-07T09:00:00+00:00"))

            self.assertIsNotNone(signal)
            self.assertEqual(signal.direction, Direction.CE)
            rows = [json.loads(line) for line in (Path(tmpdir) / "decision_traces.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["final_outcome"], "entry_taken")
            self.assertIsNone(rows[-1]["composite_depth_shadow"])


if __name__ == "__main__":
    unittest.main()
