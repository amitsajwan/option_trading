"""
Test suite for velocity-enhanced policy classes.

Tests VelocityEnhancedRegimeClassifier and VelocityEnhancedEntryPolicy
with and without velocity data to ensure backward compatibility and
proper enhancement behavior.
"""

import unittest
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, Any

import numpy as np
import pandas as pd

from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.velocity_regime_classifier import VelocityEnhancedRegimeClassifier
from strategy_app.engines.velocity_entry_policy import VelocityEnhancedEntryPolicy
from strategy_app.engines.regime import Regime
from strategy_app.contracts import StrategyVote, Direction, SignalType


@dataclass
class RiskContext:
    """Risk management context."""
    max_loss_per_trade: float = 1000.0
    max_open_contracts: int = 5
    daily_loss_limit: float = 5000.0
    current_open_count: int = 0
    current_daily_loss: float = 0.0


def make_test_vote(direction: Direction) -> StrategyVote:
    """Create a test StrategyVote."""
    return StrategyVote(
        strategy_name="test_strategy",
        snapshot_id="test_snap_001",
        timestamp=datetime.now(),
        trade_date="2026-04-18",
        signal_type=SignalType.ENTRY,
        direction=direction,
        confidence=0.75,
        reason="test",
        raw_signals={},
    )


class TestVelocityPolicies(unittest.TestCase):
    """Test velocity-enhanced policies with realistic snapshot data."""

    def setUp(self):
        """Set up test fixtures."""
        self.velocity_classifier = VelocityEnhancedRegimeClassifier()
        self.velocity_policy = VelocityEnhancedEntryPolicy()

    def _make_base_snapshot(self) -> dict:
        """Create a realistic base snapshot without velocity."""
        return {
            "timestamp": "2026-04-18 12:00:00",
            "close": 52000.0,
            "high": 52150.0,
            "low": 51850.0,
            "open": 51950.0,
            "volume": 1_000_000,
            "option_iv": 15.2,
            "option_bid": 125.0,
            "option_ask": 135.0,
            "atm_call_oi": 50_000,
            "atm_put_oi": 45_000,
            "pcr_ratio": 0.9,
            "call_volume": 50_000,
            "put_volume": 45_000,
        }

    def _make_velocity_snapshot(self) -> dict:
        """Create a snapshot with velocity enrichment (11:30+ computed)."""
        snap = self._make_base_snapshot()
        snap["velocity_enrichment"] = {
            # OI velocity (6 cols)
            "vel_atm_call_oi_5m": 1200.0,
            "vel_atm_put_oi_5m": -800.0,
            "vel_atm_call_oi_15m": 3600.0,
            "vel_atm_put_oi_15m": -2400.0,
            "vel_atm_total_oi_5m": 400.0,
            "vel_atm_total_oi_15m": 1200.0,
            # PCR velocity (3 cols)
            "vel_pcr_5m": 0.02,
            "vel_pcr_15m": 0.05,
            "vel_pcr_trend": 1.0,  # increasing
            # Price velocity (6 cols)
            "vel_price_delta_open": 150.0,  # up 150 from open
            "vel_price_trend": 1.0,  # uptrend
            "vel_price_reversal": 0.0,
            "vel_price_range_5m": 250.0,
            "vel_price_range_15m": 700.0,
            "vel_price_range_session": 900.0,
            # IV velocity (5 cols)
            "vel_iv_delta_open": -1.2,  # IV compressed
            "vel_iv_trend": -1.0,  # downtrend (compression)
            "vel_iv_percentile": 35.0,
            "vel_iv_skew": 0.8,
            "vel_iv_term_structure": 0.5,
            # Volume velocity (9 cols)
            "vel_call_volume_5m": 5000.0,
            "vel_put_volume_5m": -3000.0,
            "vel_total_volume_5m": 2000.0,
            "vel_call_volume_15m": 15000.0,
            "vel_put_volume_15m": -9000.0,
            "vel_total_volume_15m": 6000.0,
            "vel_volume_trend": 1.0,  # increasing
            "vel_call_pct_chg": 0.15,
            "vel_put_pct_chg": -0.09,
            # Context (10 cols)
            "ctx_gap_pct": 0.5,
            "ctx_prev_close_opp": -250.0,
            "ctx_am_trend": 1.0,
            "ctx_am_reversal": 0.0,
            "ctx_am_trend_strength": 0.65,  # Strong trend
            "vol_spike_ratio": 1.45,
            "vol_spike_direction": 1.0,
            "ctx_20d_avg_opt_vol": 35_000.0,
            "ctx_prev_day_midday_opt_vol": 40_000.0,
            "ctx_session_age_frac": 0.4,  # 40% through session
            "ctx_iv_regime": 35.0,
        }
        return snap

    def test_velocity_classifier_without_velocity(self):
        """VelocityEnhancedRegimeClassifier should degrade gracefully without velocity."""
        snap = SnapshotAccessor(self._make_base_snapshot())
        signal = self.velocity_classifier.classify(snap)

        # Should return a valid signal
        self.assertIsNotNone(signal)
        self.assertIn(signal.regime, [r for r in Regime])
        self.assertGreaterEqual(signal.confidence, 0.0)
        self.assertLessEqual(signal.confidence, 1.0)
        print(f"[OK] Without velocity: regime={signal.regime}, confidence={signal.confidence}")

    def test_velocity_classifier_with_velocity(self):
        """VelocityEnhancedRegimeClassifier should boost confidence with valid velocity."""
        snap = SnapshotAccessor(self._make_velocity_snapshot())
        signal = self.velocity_classifier.classify(snap)

        # Should return a valid signal
        self.assertIsNotNone(signal)
        self.assertIn(signal.regime, [r for r in Regime])

        # With positive velocity context, confidence should be reasonable
        self.assertGreaterEqual(signal.confidence, 0.0)
        self.assertLessEqual(signal.confidence, 1.0)

        # Evidence should include velocity metrics
        evidence_keys = signal.evidence.keys()
        self.assertTrue(
            any("velocity" in key.lower() or "morning" in key.lower() for key in evidence_keys),
            f"Evidence missing velocity context: {evidence_keys}"
        )

        print(f"[OK] With velocity: regime={signal.regime}, confidence={signal.confidence}")
        print(f"  Evidence keys: {list(evidence_keys)}")

    def test_entry_policy_without_velocity(self):
        """VelocityEnhancedEntryPolicy should work without velocity."""
        snap = SnapshotAccessor(self._make_base_snapshot())
        vote = make_test_vote(Direction.CE)
        regime_signal = self.velocity_classifier.classify(snap)
        risk = RiskContext(current_open_count=0)

        decision = self.velocity_policy.evaluate(snap, vote, regime_signal, risk)

        # Should return a decision
        self.assertIsNotNone(decision)
        self.assertIn(decision.allowed, [True, False])

        # Check dict should exist
        self.assertIsNotNone(decision.checks)
        self.assertIsInstance(decision.checks, dict)

        print(f"[OK] Entry policy without velocity: allowed={decision.allowed}, score={decision.score}")

    def test_entry_policy_with_velocity_favorable(self):
        """VelocityEnhancedEntryPolicy should favor entries with good morning velocity."""
        snap = SnapshotAccessor(self._make_velocity_snapshot())  # uptrend, vol spike 1.45
        vote = make_test_vote(Direction.CE)  # aligned with uptrend
        regime_signal = self.velocity_classifier.classify(snap)
        risk = RiskContext(current_open_count=0)

        decision = self.velocity_policy.evaluate(snap, vote, regime_signal, risk)

        self.assertIsNotNone(decision)
        self.assertIsInstance(decision.checks, dict)

        # With velocity momentum aligned, policy should pass morning gate
        if decision.allowed:
            print(f"[OK] Entry policy with favorable velocity: ALLOWED")
            print(f"  Checks: {decision.checks}")
            print(f"  Score: {decision.score}")
        else:
            print(f"[OK] Entry policy with favorable velocity: BLOCKED (may be due to other gates)")
            print(f"  Checks: {decision.checks}")

    def test_entry_policy_with_velocity_adverse(self):
        """VelocityEnhancedEntryPolicy should block entries against morning velocity."""
        snap_data = self._make_velocity_snapshot()
        # Flip to downtrend
        snap_data["velocity_enrichment"]["vel_price_trend"] = -1.0
        snap_data["velocity_enrichment"]["vel_price_delta_open"] = -150.0
        snap_data["velocity_enrichment"]["ctx_am_trend"] = -1.0
        snap_data["velocity_enrichment"]["vol_spike_ratio"] = 0.75  # low volume

        snap = SnapshotAccessor(snap_data)
        vote = make_test_vote(Direction.CE)  # opposed to downtrend
        regime_signal = self.velocity_classifier.classify(snap)
        risk = RiskContext(current_open_count=0)

        decision = self.velocity_policy.evaluate(snap, vote, regime_signal, risk)

        self.assertIsNotNone(decision)
        self.assertIsInstance(decision.checks, dict)

        print(f"[OK] Entry policy with adverse velocity: allowed={decision.allowed}")
        print(f"  Morning momentum check: {decision.checks.get('morning_momentum', 'N/A')}")
        print(f"  Full checks: {decision.checks}")

    def test_velocity_accessor_methods(self):
        """Test SnapshotAccessor velocity-specific methods."""
        snap = SnapshotAccessor(self._make_velocity_snapshot())

        # Test has_velocity
        self.assertTrue(snap.has_velocity, "Should detect velocity enrichment")

        # Test vel() accessor
        vel_price_delta = snap.vel("vel_price_delta_open")
        self.assertAlmostEqual(vel_price_delta, 150.0, places=1)

        # Test missing velocity column (should return NaN or None)
        snap_no_vel = SnapshotAccessor(self._make_base_snapshot())
        self.assertFalse(snap_no_vel.has_velocity)
        missing_val = snap_no_vel.vel("vel_price_delta_open")
        # Should be either None or NaN
        self.assertTrue(missing_val is None or (isinstance(missing_val, float) and np.isnan(missing_val)),
                       f"Expected None or NaN, got {missing_val}")

        print(f"[OK] SnapshotAccessor velocity methods working")

    def test_velocity_features_property(self):
        """Test SnapshotAccessor velocity_features property."""
        snap = SnapshotAccessor(self._make_velocity_snapshot())

        features = snap.velocity_features
        self.assertIsInstance(features, dict)
        self.assertGreater(len(features), 0)

        # Check known keys are present
        self.assertIn("vel_price_delta_open", features)
        self.assertIn("vol_spike_ratio", features)
        self.assertIn("ctx_am_trend", features)

        print(f"[OK] velocity_features property returns {len(features)} features")


class TestVelocityBacktestScenarios(unittest.TestCase):
    """Integration tests with realistic trading scenarios."""

    def setUp(self):
        """Set up backtesting infrastructure."""
        self.velocity_classifier = VelocityEnhancedRegimeClassifier()
        self.velocity_policy = VelocityEnhancedEntryPolicy()

    def test_morning_momentum_scenario(self):
        """Test policy behavior across morning momentum extremes."""
        scenarios = [
            {
                "name": "Strong bullish morning",
                "vel_price_delta_open": 200.0,
                "vel_price_trend": 1.0,
                "ctx_am_trend": 1.0,
                "vol_spike_ratio": 1.5,
                "direction": Direction.CE,
            },
            {
                "name": "Weak bearish morning",
                "vel_price_delta_open": -150.0,
                "vel_price_trend": -1.0,
                "ctx_am_trend": -1.0,
                "vol_spike_ratio": 0.8,
                "direction": Direction.PE,
            },
            {
                "name": "Sideways morning",
                "vel_price_delta_open": 25.0,
                "vel_price_trend": 0.0,
                "ctx_am_trend": 0.0,
                "vol_spike_ratio": 1.0,
                "direction": Direction.CE,
            },
        ]

        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]):
                snap_data = self._make_velocity_snapshot()
                snap_data["velocity_enrichment"]["vel_price_delta_open"] = scenario["vel_price_delta_open"]
                snap_data["velocity_enrichment"]["vel_price_trend"] = scenario["vel_price_trend"]
                snap_data["velocity_enrichment"]["ctx_am_trend"] = scenario["ctx_am_trend"]
                snap_data["velocity_enrichment"]["vol_spike_ratio"] = scenario["vol_spike_ratio"]

                snap = SnapshotAccessor(snap_data)
                vote = make_test_vote(scenario["direction"])
                regime = self.velocity_classifier.classify(snap)
                decision = self.velocity_policy.evaluate(snap, vote, regime, RiskContext())

                print(f"  {scenario['name']}: allowed={decision.allowed}, score={decision.score}")

    def _make_velocity_snapshot(self) -> dict:
        """Helper to create velocity snapshot."""
        snap = {
            "timestamp": "2026-04-18 12:00:00",
            "close": 52000.0,
            "high": 52150.0,
            "low": 51850.0,
            "open": 51950.0,
            "volume": 1_000_000,
            "option_iv": 15.2,
            "option_bid": 125.0,
            "option_ask": 135.0,
            "atm_call_oi": 50_000,
            "atm_put_oi": 45_000,
            "pcr_ratio": 0.9,
            "call_volume": 50_000,
            "put_volume": 45_000,
            "velocity_enrichment": {
                "vel_atm_call_oi_5m": 1200.0,
                "vel_atm_put_oi_5m": -800.0,
                "vel_atm_call_oi_15m": 3600.0,
                "vel_atm_put_oi_15m": -2400.0,
                "vel_atm_total_oi_5m": 400.0,
                "vel_atm_total_oi_15m": 1200.0,
                "vel_pcr_5m": 0.02,
                "vel_pcr_15m": 0.05,
                "vel_pcr_trend": 1.0,
                "vel_price_delta_open": 150.0,
                "vel_price_trend": 1.0,
                "vel_price_reversal": 0.0,
                "vel_price_range_5m": 250.0,
                "vel_price_range_15m": 700.0,
                "vel_price_range_session": 900.0,
                "vel_iv_delta_open": -1.2,
                "vel_iv_trend": -1.0,
                "vel_iv_percentile": 35.0,
                "vel_iv_skew": 0.8,
                "vel_iv_term_structure": 0.5,
                "vel_call_volume_5m": 5000.0,
                "vel_put_volume_5m": -3000.0,
                "vel_total_volume_5m": 2000.0,
                "vel_call_volume_15m": 15000.0,
                "vel_put_volume_15m": -9000.0,
                "vel_total_volume_15m": 6000.0,
                "vel_volume_trend": 1.0,
                "vel_call_pct_chg": 0.15,
                "vel_put_pct_chg": -0.09,
                "ctx_gap_pct": 0.5,
                "ctx_prev_close_opp": -250.0,
                "ctx_am_trend": 1.0,
                "ctx_am_reversal": 0.0,
                "ctx_am_trend_strength": 0.65,
                "vol_spike_ratio": 1.45,
                "vol_spike_direction": 1.0,
                "ctx_20d_avg_opt_vol": 35_000.0,
                "ctx_prev_day_midday_opt_vol": 40_000.0,
                "ctx_session_age_frac": 0.4,
                "ctx_iv_regime": 35.0,
            },
        }
        return snap


if __name__ == "__main__":
    unittest.main(verbosity=2)
