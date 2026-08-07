"""ML shadow bookkeeping: probability + direction get logged even when the
regime gate excludes ML_ENTRY from the active strategy set (PRE_EXPIRY/CHOP).
Read-only -- can never become a real trade. See _build_ml_shadow_vote().
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from strategy_app.contracts import Direction, SignalType
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.logging.signal_logger import SignalLogger
from strategy_app.market.regime import Regime, RegimeSignal


class _AllowPolicy:
    def evaluate(self, snap, vote, regime, risk):
        from strategy_app.policy.entry_policy import EntryPolicyDecision
        return EntryPolicyDecision(allowed=True, score=0.9, checks={})


def _regime_signal(regime: Regime) -> RegimeSignal:
    return RegimeSignal(regime=regime, confidence=1.0, reason="test", evidence={})


def _snapshot_payload() -> dict:
    return {
        "snapshot_id": "snap-shadow-ml-1",
        "timestamp": "2024-08-15T06:00:00+00:00",
        "trade_date": "2024-08-15",
        "atm_strike": 52000,
        "atm_ce_close": 120.0,
        "atm_pe_close": 115.0,
        "fut_return_5m": 0.002,
    }


class MlShadowBookkeepingTest(unittest.TestCase):
    def _engine(self, tmpdir):
        return DeterministicRuleEngine(signal_logger=SignalLogger(run_dir=tmpdir), entry_policy=_AllowPolicy())

    def test_off_by_default_no_shadow_vote(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self._engine(d)
            self.assertFalse(engine._ml_score_all_snapshots)
            vote = engine._build_ml_shadow_vote(
                snapshot=_snapshot_payload(), position=None, risk=MagicMock(),
                regime_signal=_regime_signal(Regime.PRE_EXPIRY),
            )
            self.assertIsNone(vote)

    def test_position_open_no_shadow_vote(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self._engine(d)
            engine._ml_score_all_snapshots = True
            vote = engine._build_ml_shadow_vote(
                snapshot=_snapshot_payload(), position=MagicMock(), risk=MagicMock(),
                regime_signal=_regime_signal(Regime.PRE_EXPIRY),
            )
            self.assertIsNone(vote)

    def test_regime_blocked_still_scores_probability_and_direction(self):
        """The real gap this closes: PRE_EXPIRY/CHOP exclude ML_ENTRY from the
        router's active set, so its probability was never computed at all.
        With ml_score_all_snapshots on, it must still run -- for bookkeeping
        only, never for a real trade."""
        with tempfile.TemporaryDirectory() as d:
            engine = self._engine(d)
            engine._ml_score_all_snapshots = True
            regime_signal = _regime_signal(Regime.PRE_EXPIRY)
            # Confirm the premise rather than assume it: PRE_EXPIRY really does
            # exclude ML_ENTRY in the production profile map.
            active = engine._router.get_strategies(regime_signal.regime, None)
            self.assertNotIn(engine._router.get_strategy("ML_ENTRY"), active)

            bundle = {
                "kind": "entry_only_bundle",
                "features": ["fut_return_5m"],
                "feature_medians": {"fut_return_5m": 0.0},
                "model": MagicMock(),
            }
            with patch("strategy_app.engines.strategies.ml_entry.load_joblib_bundle", return_value=bundle), \
                 patch("strategy_app.engines.strategies.ml_entry.predict_positive_class_prob", return_value=0.90), \
                 patch.dict(os.environ, {
                     "ENTRY_ML_MODEL_PATH": "/fake/entry.joblib",
                     "ENTRY_ML_MIN_PROB": "0.50",
                     "ML_ENTRY_DIRECTION_MODE": "momentum",
                 }):
                vote = engine._build_ml_shadow_vote(
                    snapshot=_snapshot_payload(), position=None, risk=MagicMock(),
                    regime_signal=regime_signal,
                )

        self.assertIsNotNone(vote)
        self.assertEqual(vote.signal_type, SignalType.SKIP)  # never actionable
        self.assertEqual(vote.direction, Direction.CE)
        self.assertGreaterEqual(vote.confidence, 0.90)
        self.assertTrue(vote.raw_signals.get("_ml_shadow"))
        self.assertEqual(vote.raw_signals.get("_ml_shadow_reason"), "regime_blocked:PRE_EXPIRY")

    def test_already_active_regime_skips_double_evaluation(self):
        """When the regime already includes ML_ENTRY in its live strategy set,
        _collect_votes evaluates it for real -- the shadow path must not also
        run it a second time (double model inference / diag overwrite)."""
        with tempfile.TemporaryDirectory() as d:
            engine = self._engine(d)
            engine._ml_score_all_snapshots = True
            trigger = engine._router.get_strategy("ML_ENTRY")
            with patch.object(engine._router, "get_strategies", return_value=[trigger]), \
                 patch.object(trigger, "evaluate") as mock_eval:
                vote = engine._build_ml_shadow_vote(
                    snapshot=_snapshot_payload(), position=None, risk=MagicMock(),
                    regime_signal=_regime_signal(Regime.TRENDING),
                )
        mock_eval.assert_not_called()
        self.assertIsNone(vote)

    def test_shadow_vote_survives_the_no_votes_early_return(self):
        """The real gap (found 2026-08-05, live): _evaluate_impl computes the
        shadow vote BEFORE checking `if not votes:`, but that branch used to
        return early and never reach the log_vote()/candidate-building code --
        so for CHOP/AVOID (where votes is ALWAYS empty, unlike PRE_EXPIRY which
        can still have other active strategies), the shadow vote was silently
        computed and discarded. Turning on ML_SCORE_ALL_SNAPSHOTS delivered
        nothing for exactly the regimes it exists to observe. This exercises
        the real end-to-end evaluate() path, not just _build_ml_shadow_vote()
        in isolation (which is what let the gap through the first time)."""
        with tempfile.TemporaryDirectory() as d:
            engine = self._engine(d)
            engine._ml_score_all_snapshots = True
            regime_signal = _regime_signal(Regime.CHOP)

            trigger = engine._router.get_strategy("ML_ENTRY")
            from datetime import datetime, timezone
            from strategy_app.contracts import StrategyVote
            shadow_vote = StrategyVote(
                strategy_name="ML_ENTRY", snapshot_id="snap-shadow-ml-1",
                timestamp=datetime(2024, 8, 15, 6, 0, tzinfo=timezone.utc), trade_date="2024-08-15",
                signal_type=SignalType.ENTRY, direction=Direction.CE, confidence=0.75,
                reason="ml_entry prob=0.75", decision_metrics={"entry_prob": 0.75},
                raw_signals={},
            )
            with patch.object(engine, "_regime") as mock_regime, \
                 patch.object(engine._router, "get_strategies", return_value=[]), \
                 patch.object(trigger, "evaluate", return_value=shadow_vote), \
                 patch.object(engine, "_collect_votes", return_value=[]):
                mock_regime.classify.return_value = regime_signal
                engine.evaluate(_snapshot_payload())

        trace = engine.last_decision_trace
        self.assertIsInstance(trace, dict)
        shadow_candidates = [c for c in trace.get("candidates", []) if c.get("candidate_type") == "ml_shadow"]
        self.assertEqual(len(shadow_candidates), 1, f"expected exactly 1 ml_shadow candidate, trace candidates={trace.get('candidates')}")
        cand = shadow_candidates[0]
        self.assertEqual(cand["direction"], "CE")
        self.assertEqual(cand["terminal_status"], "shadow_only")
        self.assertFalse(cand["selected"])  # never actionable

    def test_ml_score_all_snapshots_survives_per_bar_set_run_context(self):
        """The real gap (found 2026-08-07, live): redis_snapshot_consumer.py
        calls engine.set_run_context(run_id, metadata) on EVERY live bar, but
        `metadata` there is the snapshot event's own metadata -- it never
        carries ml_score_all_snapshots (that key only exists in main.py's
        startup run_metadata). set_run_context used to unconditionally do
        `self._ml_score_all_snapshots = as_bool(metadata.get(...))`, which is
        as_bool(None) == False -- silently resetting the flag to False on the
        very first live bar after startup. So even after fixing the shadow
        vote to survive the no-votes early return, ML_SCORE_ALL_SNAPSHOTS=1
        never actually produced a shadow candidate live: the flag was already
        False by the time any bar past the first got evaluated."""
        with tempfile.TemporaryDirectory() as d:
            engine = self._engine(d)
            # Startup call: main.py passes the full run_metadata dict, which
            # does carry the key.
            engine.set_run_context("run-1", {"ml_score_all_snapshots": True})
            self.assertTrue(engine._ml_score_all_snapshots)

            # Per-bar call: redis_snapshot_consumer.py passes the snapshot
            # event's own metadata -- shaped like a real event, but with no
            # ml_score_all_snapshots key at all.
            engine.set_run_context("run-1", {"risk_config": {"stop_loss_pct": None}})
            self.assertTrue(
                engine._ml_score_all_snapshots,
                "flag must survive a set_run_context call whose metadata lacks the key",
            )

            # Explicit False must still be honored (real config bounce).
            engine.set_run_context("run-1", {"ml_score_all_snapshots": False})
            self.assertFalse(engine._ml_score_all_snapshots)

    def test_never_raises_on_strategy_exception(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self._engine(d)
            engine._ml_score_all_snapshots = True
            trigger = engine._router.get_strategy("ML_ENTRY")
            with patch.object(engine._router, "get_strategies", return_value=[]), \
                 patch.object(trigger, "evaluate", side_effect=RuntimeError("boom")):
                vote = engine._build_ml_shadow_vote(
                    snapshot=_snapshot_payload(), position=None, risk=MagicMock(),
                    regime_signal=_regime_signal(Regime.PRE_EXPIRY),
                )
        self.assertIsNone(vote)


if __name__ == "__main__":
    unittest.main()
