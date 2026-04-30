"""
Velocity Policy Testing Service - Backtest velocity-enhanced policies over date ranges.

Provides API endpoints to test VelocityEnhancedRegimeClassifier and
VelocityEnhancedEntryPolicy against historical data.
"""

import pandas as pd
from datetime import datetime, date
from typing import Optional, Dict, Any, List
from dataclasses import asdict
import logging

from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.velocity_regime_classifier import VelocityEnhancedRegimeClassifier
from strategy_app.engines.velocity_entry_policy import VelocityEnhancedEntryPolicy
from strategy_app.contracts import StrategyVote, Direction, SignalType

logger = logging.getLogger(__name__)


class VelocityTestingService:
    """Service for testing velocity-enhanced policies on historical data."""

    def __init__(self, data_provider=None):
        """Initialize with optional data provider for loading historical snapshots."""
        self.data_provider = data_provider
        self.velocity_classifier = VelocityEnhancedRegimeClassifier()
        self.velocity_policy = VelocityEnhancedEntryPolicy()

    def test_policies_for_date_range(
        self,
        date_from: str,
        date_to: str,
        trade_direction: Optional[str] = None,
        min_velocity_score: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Test velocity-enhanced policies over a date range.
        
        Args:
            date_from: Start date (YYYY-MM-DD)
            date_to: End date (YYYY-MM-DD)
            trade_direction: Optional filter for Direction.CE or Direction.PE
            min_velocity_score: Minimum regime confidence to include in results
            
        Returns:
            Dictionary with test results including:
            - summary: counts of tests run, passed gates, blocked, etc.
            - results: list of detailed test results per snapshot
            - statistics: descriptive stats on policy decisions
        """
        if not self.data_provider:
            raise ValueError("Data provider not configured for velocity testing")

        # Parse dates
        try:
            start_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            end_date = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError as e:
            raise ValueError(f"Invalid date format. Use YYYY-MM-DD: {e}")

        if start_date > end_date:
            raise ValueError("date_from must be before date_to")

        # Load snapshots for date range
        snapshots = self.data_provider.get_snapshots_for_date_range(start_date, end_date)
        
        if not snapshots:
            return {
                "summary": {
                    "total_tests": 0,
                    "error": "No snapshots found for date range",
                },
                "results": [],
                "statistics": {},
            }

        # Run tests
        results = []
        regime_scores = []
        policy_decisions = []
        morning_momentum_passes = 0
        iv_quality_passes = 0

        for snap_dict in snapshots:
            snap = SnapshotAccessor(snap_dict)
            
            # Skip if velocity not available (before 11:30)
            if not snap.has_velocity:
                continue

            # Test regime classifier
            regime_signal = self.velocity_classifier.classify(snap)
            regime_scores.append(regime_signal.confidence)

            # Create a test vote for the policy
            vote = StrategyVote(
                strategy_name="velocity_test",
                snapshot_id=snap_dict.get("snapshot_id", "unknown"),
                timestamp=datetime.fromisoformat(str(snap_dict.get("timestamp", datetime.now()))),
                trade_date=str(snap_dict.get("trade_date", date.today())),
                signal_type=SignalType.ENTRY,
                direction=Direction.CE if trade_direction != "PE" else Direction.PE,
                confidence=0.75,
                reason="velocity_test",
                raw_signals={},
            )

            # Skip if direction filter set and doesn't match
            if trade_direction and str(vote.direction) != f"Direction.{trade_direction}":
                continue

            # Test entry policy
            riskcontext = self._mock_risk_context()
            decision = self.velocity_policy.evaluate(snap, vote, regime_signal, riskcontext)

            policy_decisions.append(decision.allowed)

            # Extract gate decisions
            checks = decision.checks or {}
            if "morning_momentum" in checks and "PASS" in str(checks["morning_momentum"]):
                morning_momentum_passes += 1
            if "iv_quality" in checks and "PASS" in str(checks["iv_quality"]):
                iv_quality_passes += 1

            results.append({
                "timestamp": str(snap_dict.get("timestamp")),
                "regime": regime_signal.regime.value,
                "regime_confidence": round(regime_signal.confidence, 3),
                "regime_reason": regime_signal.reason,
                "entry_allowed": decision.allowed,
                "entry_score": round(decision.score, 3),
                "entry_reason": decision.reason,
                "checks": {k: str(v) for k, v in checks.items()},
                "has_velocity": snap.has_velocity,
            })

        # Compute statistics
        statistics = {
            "total_snapshots_with_velocity": len(results),
            "avg_regime_confidence": round(sum(regime_scores) / len(regime_scores), 3) if regime_scores else 0.0,
            "min_regime_confidence": round(min(regime_scores), 3) if regime_scores else 0.0,
            "max_regime_confidence": round(max(regime_scores), 3) if regime_scores else 0.0,
            "entries_allowed": sum(policy_decisions),
            "entries_blocked": len(policy_decisions) - sum(policy_decisions),
            "allow_rate_pct": round(100 * sum(policy_decisions) / len(policy_decisions), 1) if policy_decisions else 0.0,
            "morning_momentum_passed": morning_momentum_passes,
            "iv_quality_passed": iv_quality_passes,
        }

        return {
            "summary": {
                "date_from": date_from,
                "date_to": date_to,
                "total_tests": len(results),
                "trade_direction": trade_direction,
            },
            "results": results[-100:],  # Return last 100 results for UI display
            "statistics": statistics,
        }

    def _mock_risk_context(self):
        """Create mock risk context for testing."""
        from strategy_app.contracts import RiskContext
        return {
            "max_loss_per_trade": 1000.0,
            "max_open_contracts": 5,
            "daily_loss_limit": 5000.0,
            "current_open_count": 0,
            "current_daily_loss": 0.0,
        }

    def get_velocity_heatmap(
        self,
        date_from: str,
        date_to: str,
    ) -> Dict[str, Any]:
        """
        Generate a heatmap of velocity metrics across date range.
        
        Shows distribution of velocity signals to understand which conditions
        favor entries and which block them.
        """
        if not self.data_provider:
            raise ValueError("Data provider not configured")

        start_date = datetime.strptime(date_from, "%Y-%m-%d").date()
        end_date = datetime.strptime(date_to, "%Y-%m-%d").date()

        snapshots = self.data_provider.get_snapshots_for_date_range(start_date, end_date)

        if not snapshots:
            return {
                "error": "No snapshots found for date range",
            }

        # Collect velocity metrics
        metrics_data = []
        for snap_dict in snapshots:
            snap = SnapshotAccessor(snap_dict)
            if not snap.has_velocity:
                continue

            vel_features = snap.velocity_features
            metrics_data.append({
                "timestamp": snap_dict.get("timestamp"),
                "price_delta_open": vel_features.get("vel_price_delta_open"),
                "price_trend": vel_features.get("vel_price_trend"),
                "am_trend": vel_features.get("ctx_am_trend"),
                "vol_spike": vel_features.get("vol_spike_ratio"),
                "iv_trend": vel_features.get("vel_iv_trend"),
                "oi_buildup": vel_features.get("vel_atm_total_oi_5m"),
            })

        if not metrics_data:
            return {"error": "No velocity data found"}

        df = pd.DataFrame(metrics_data)
        
        return {
            "summary": {
                "date_from": date_from,
                "date_to": date_to,
                "snapshots_analyzed": len(metrics_data),
            },
            "distributions": {
                "price_delta_open": {
                    "mean": float(df["price_delta_open"].mean()),
                    "std": float(df["price_delta_open"].std()),
                    "min": float(df["price_delta_open"].min()),
                    "max": float(df["price_delta_open"].max()),
                },
                "vol_spike_ratio": {
                    "mean": float(df["vol_spike"].mean()),
                    "std": float(df["vol_spike"].std()),
                    "min": float(df["vol_spike"].min()),
                    "max": float(df["vol_spike"].max()),
                },
            },
            "samples": metrics_data[-20:],  # Last 20 samples
        }
