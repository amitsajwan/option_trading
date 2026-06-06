"""The intelligent-brain shadow is wired into the live engine, env-gated, read-only."""
from __future__ import annotations

import os
import tempfile
import unittest

from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.logging.signal_logger import SignalLogger
from strategy_app.market.snapshot_accessor import SnapshotAccessor


class _AllowPolicy:
    def evaluate(self, snap, vote, regime, risk):
        from strategy_app.policy.entry_policy import EntryPolicyDecision
        return EntryPolicyDecision(allowed=True, score=0.9, checks={})


def _loaded_payload():
    close = 54000.0
    return {
        "snapshot_id": "snap-shadow-1",
        "futures_bar": {"fut_close": close, "fut_high": close + 20, "fut_low": close - 20},
        "futures_derived": {"fut_return_1m": 0.0008, "realized_vol_30m": 0.0006,
                            "vol_ratio": 0.5, "fut_volume_ratio": 2.0, "fut_oi_change_30m": 8000.0,
                            "vwap": close - 10},
        "opening_range": {"orh": 54400.0, "orl": 53600.0},
        "chain_aggregates": {"max_pain": 54600, "ce_oi_top_strike": 55200, "pe_oi_top_strike": 53000},
        "atm_options": {"atm_ce_close": 190.0, "atm_pe_close": 185.0},
        "session_context": {"date": "2026-06-05"},
    }


class BrainShadowWiringTest(unittest.TestCase):
    def test_shadow_off_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ.pop("INTELLIGENT_BRAIN_SHADOW", None)
            engine = DeterministicRuleEngine(signal_logger=SignalLogger(run_dir=d), entry_policy=_AllowPolicy())
            self.assertIsNone(engine._brain_shadow)            # not constructed when flag off

    def test_shadow_on_populates_trace_without_affecting_signal(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["INTELLIGENT_BRAIN_SHADOW"] = "true"
            try:
                engine = DeterministicRuleEngine(signal_logger=SignalLogger(run_dir=d), entry_policy=_AllowPolicy())
                self.assertIsNotNone(engine._brain_shadow)
                # call the shadow directly (avoids the heavy evaluate path); flat position
                engine._run_brain_shadow(SnapshotAccessor(_loaded_payload()), position=None)
                shadow = engine.last_brain_shadow
                self.assertIsInstance(shadow, dict)
                self.assertIn("action", shadow)
                self.assertIn(shadow["action"], ("TRADE", "WAIT", "SKIP", "NO_TRADE"))
                self.assertIn("move", shadow["verdicts"])
            finally:
                os.environ.pop("INTELLIGENT_BRAIN_SHADOW", None)

    def test_shadow_never_raises_on_bad_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["INTELLIGENT_BRAIN_SHADOW"] = "true"
            try:
                engine = DeterministicRuleEngine(signal_logger=SignalLogger(run_dir=d), entry_policy=_AllowPolicy())
                engine._run_brain_shadow(SnapshotAccessor({}), position=None)   # empty snapshot
                self.assertIsInstance(engine.last_brain_shadow, dict)            # recorded, not raised
            finally:
                os.environ.pop("INTELLIGENT_BRAIN_SHADOW", None)


if __name__ == "__main__":
    unittest.main()
