"""Enhanced regime classification leveraging velocity features (morning momentum context).

Instead of post-11:30 reactive signals, use pre-computed velocity to improve
regime confidence and detect trending vs sideways with early morning data.

Usage:
    Enhanced regime adds velocity_score (0-1) to RegimeSignal.evidence dict,
    giving entry policy better context for pre-11:30 conditions that shape the day.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from .regime import Regime, RegimeClassifier, RegimeSignal
from .snapshot_accessor import SnapshotAccessor

logger = logging.getLogger(__name__)


class VelocityEnhancedRegimeClassifier(RegimeClassifier):
    """Regime classification enhanced with velocity features for morning context."""

    def _rule_classify(self, snap: SnapshotAccessor) -> RegimeSignal:
        """Classify regime, then enhance with velocity signals if available."""
        # Base regime classification (unchanged)
        base_signal = super()._rule_classify(snap)

        # If we have velocity (post-11:30), enhance the signal
        if snap.has_velocity:
            base_signal = self._enhance_with_velocity(snap, base_signal)

        return base_signal

    def _enhance_with_velocity(self, snap: SnapshotAccessor, base_signal: RegimeSignal) -> RegimeSignal:
        """Boost confidence and add velocity context to the regime signal."""
        vel_metrics = self._compute_velocity_metrics(snap)

        # Trending enhancement: strong velocity + no reversal = high confidence
        if base_signal.regime == Regime.TRENDING:
            if vel_metrics["momentum_strength"] > 0.70 and not vel_metrics["has_reversal"]:
                confidence_boost = 0.08
                base_signal.confidence = min(0.99, base_signal.confidence + confidence_boost)
                base_signal.evidence["velocity_boost_trending"] = confidence_boost

        # Sideways enhancement: weak velocity + high range = validate sideways
        elif base_signal.regime == Regime.SIDEWAYS:
            if vel_metrics["momentum_strength"] < 0.40 and vel_metrics["price_range_size"] > 0.005:
                confidence_boost = 0.05
                base_signal.confidence = min(0.95, base_signal.confidence + confidence_boost)
                base_signal.evidence["velocity_confirms_sideways"] = True

        # Add velocity metrics to evidence for observability
        base_signal.evidence.update({
            "morning_momentum_strength": vel_metrics["momentum_strength"],
            "morning_reversal_flag": vel_metrics["has_reversal"],
            "morning_range_size": vel_metrics["price_range_size"],
            "oi_buildup_asymmetry": vel_metrics["oi_asymmetry"],
            "morning_trend_direction": vel_metrics["trend_direction"],
            "vol_spike_today": vel_metrics["vol_spike_ratio"],
        })

        return base_signal

    def _compute_velocity_metrics(self, snap: SnapshotAccessor) -> dict[str, Any]:
        """Extract and normalize velocity features into interpretable metrics."""
        metrics = {
            "momentum_strength": 0.0,
            "has_reversal": False,
            "price_range_size": 0.0,
            "oi_asymmetry": 0.0,
            "trend_direction": 0,  # -1 (down), 0 (flat), 1 (up)
            "vol_spike_ratio": 1.0,
        }

        if not snap.has_velocity:
            return metrics

        # 1. Momentum strength from 30m acceleration
        vel_accel = snap.vel("vel_price_acceleration")
        delta_30m = snap.vel("vel_price_delta_30m")
        if delta_30m is not None and not math.isnan(delta_30m):
            fut_close = snap.fut_close or 1.0
            normalized_delta = abs(delta_30m) / fut_close  # % move
            # 0.5% = 0.5 strength, 1% = 0.8 strength (log scale)
            metrics["momentum_strength"] = min(1.0, math.log(1.0 + normalized_delta * 100) / 5.0)
            if vel_accel is not None and not math.isnan(vel_accel) and vel_accel > 0:
                metrics["momentum_strength"] *= 1.1  # Boost if accelerating

        # 2. Reversal pattern (did direction shift in last 30m?)
        reversal = snap.vel("ctx_am_reversal")
        if reversal is not None and not math.isnan(reversal):
            metrics["has_reversal"] = bool(reversal > 0.5)

        # 3. Morning range size (volatility context)
        range_high = snap.vel("ctx_am_range_high")
        range_low = snap.vel("ctx_am_range_low")
        if range_high is not None and range_low is not None and not math.isnan(range_high) and not math.isnan(range_low):
            fut_close = snap.fut_close or 1.0
            metrics["price_range_size"] = (range_high - range_low) / fut_close

        # 4. OI asymmetry (directional bias)
        ce_oi_delta = snap.vel("vel_ce_oi_delta_open")
        pe_oi_delta = snap.vel("vel_pe_oi_delta_open")
        if ce_oi_delta is not None and pe_oi_delta is not None and not math.isnan(ce_oi_delta) and not math.isnan(pe_oi_delta):
            total_oi_delta = abs(ce_oi_delta) + abs(pe_oi_delta)
            if total_oi_delta > 0:
                metrics["oi_asymmetry"] = abs(ce_oi_delta - pe_oi_delta) / total_oi_delta

        # 5. Trend direction from morning (1 = uptrend, 0 = flat, -1 = downtrend)
        trend = snap.vel("ctx_am_trend")
        if trend is not None and not math.isnan(trend):
            metrics["trend_direction"] = int(trend)

        # 6. Volume spike (vol_spike_ratio relative to 20d avg)
        vol_spike = snap.vel("vol_spike_ratio")
        if vol_spike is not None and not math.isnan(vol_spike):
            metrics["vol_spike_ratio"] = float(vol_spike)

        return metrics
