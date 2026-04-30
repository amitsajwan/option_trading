"""Velocity-enhanced entry policy that uses morning momentum as pre-entry gate.

Instead of only checking post-11:30 momentum (5m/15m returns), incorporate
velocity features that capture market quality and direction during 10:00-11:30,
before most entries actually happen.

This makes entry decisions more principled: we're not reacting to market movements
at entry time, but evaluating pre-computed market setup from the morning session.
"""

from __future__ import annotations

import math
from typing import Optional

from ..contracts import Direction, RiskContext, StrategyVote
from .entry_policy import EntryPolicyDecision, LongOptionEntryPolicy, PolicyConfig
from .regime import RegimeSignal
from .snapshot_accessor import SnapshotAccessor


class VelocityEnhancedEntryPolicy(LongOptionEntryPolicy):
    """Entry policy enhanced with velocity-based morning momentum gate."""

    def evaluate(
        self,
        snap: SnapshotAccessor,
        vote: StrategyVote,
        regime: RegimeSignal,
        risk: RiskContext,
    ) -> EntryPolicyDecision:
        """Enhanced evaluation that gates on velocity before checking other factors."""
        detail: dict[str, str] = {}
        score = 1.0

        # Gate 1: Morning Momentum (pre-11:30 velocity context)
        # This is new — check if the morning setup is tradable before evaluating post-11:30 conditions
        if snap.has_velocity:
            morning_result, morning_delta = self._check_morning_momentum(snap, vote)
            detail["morning_momentum"] = morning_result
            if morning_result.startswith("BLOCK"):
                return EntryPolicyDecision.block(f"morning_momentum: {morning_result}", detail)
            score += morning_delta

        # Gate 2: Volatility quality (using IV velocity)
        if snap.has_velocity:
            vol_quality_result, vol_quality_delta = self._check_volatility_quality(snap)
            detail["iv_quality"] = vol_quality_result
            if vol_quality_result.startswith("BLOCK"):
                return EntryPolicyDecision.block(f"iv_quality: {vol_quality_result}", detail)
            score += vol_quality_delta

        # All other checks (original policy)
        volume_result, volume_delta = self._check_volume(snap)
        detail["volume"] = volume_result
        if volume_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"volume: {volume_result}", detail)
        score += volume_delta

        option_liquidity_result, option_liquidity_delta = self._check_option_liquidity(snap, vote)
        detail["option_liquidity"] = option_liquidity_result
        if option_liquidity_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"option_liquidity: {option_liquidity_result}", detail)
        score += option_liquidity_delta

        momentum_result, momentum_delta = self._check_momentum(snap, vote)
        detail["momentum"] = momentum_result
        if momentum_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"momentum: {momentum_result}", detail)
        score += momentum_delta

        timing_result, timing_delta = self._check_timing(snap, vote)
        detail["timing"] = timing_result
        if timing_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"timing: {timing_result}", detail)
        score += timing_delta

        premium_result, premium_delta = self._check_premium(snap, vote)
        detail["premium"] = premium_result
        if premium_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"premium: {premium_result}", detail)
        score += premium_delta

        regime_result, regime_delta = self._check_regime(regime, vote)
        detail["regime"] = regime_result
        if regime_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"regime: {regime_result}", detail)
        score += regime_delta

        final_score = max(0.30, min(1.0, score))
        if final_score < self._cfg.min_policy_score:
            return EntryPolicyDecision.block(
                f"score: final_score={final_score:.2f}<{self._cfg.min_policy_score:.2f}",
                detail,
            )
        return EntryPolicyDecision.allow(
            f"allowed score={final_score:.2f}",
            score=final_score,
            checks=detail,
        )

    def _check_morning_momentum(self, snap: SnapshotAccessor, vote: StrategyVote) -> tuple[str, float]:
        """Gate on morning session momentum in the vote direction.

        Pre-computed velocity from 10:00-11:30 tells us if the day favors
        upside (CE) or downside (PE) momentum. We cross-check the vote
        against actual morning momentum for alignment.
        """
        direction = vote.direction
        if direction not in (Direction.CE, Direction.PE):
            return "BLOCK:unsupported_direction", 0.0

        # Price momentum from 10:00-11:30
        price_delta_open = snap.vel("vel_price_delta_open")
        if price_delta_open is None or math.isnan(price_delta_open):
            return "PASS:no_velocity_data", 0.0

        fut_close = snap.fut_close or 1.0
        sign = 1.0 if direction == Direction.CE else -1.0
        directional_momentum = sign * (price_delta_open / fut_close)

        # Morning trend alignment (ctx_am_trend = 1 for up, -1 for down, 0 for flat)
        trend_direction = snap.vel("ctx_am_trend")
        trend_strength = snap.vel("ctx_am_trend_strength")

        if trend_direction is None or math.isnan(trend_direction):
            return "PASS:no_trend_data", 0.0

        trend_alignment = sign * float(trend_direction)
        strength_score = 0.0

        if trend_alignment > 0.5:  # Vote aligns with morning trend
            if trend_strength is not None and not math.isnan(trend_strength):
                strength_score = min(0.15, float(trend_strength) * 2.0)
                return f"PASS:aligned_with_trend dir={direction} strength={trend_strength:.3f}", strength_score
            else:
                return f"PASS:aligned_with_trend dir={direction}", 0.05

        elif trend_alignment < -0.5:  # Vote opposes morning trend
            trend_str = f"{trend_direction:.0f}" if trend_direction is not None and not math.isnan(trend_direction) else "unknown"
            return f"BLOCK:against_morning_trend dir={direction} trend={trend_str}", 0.0

        else:  # Flat trend, check raw momentum strength
            if abs(directional_momentum) > 0.003:  # >0.3% raw move in vote direction
                return f"PASS:flat_morning_with_momentum delta={directional_momentum:.4f}", 0.05
            return f"WARN:flat_trend_weak_momentum delta={directional_momentum:.4f}", -0.05

    def _check_volatility_quality(self, snap: SnapshotAccessor) -> tuple[str, float]:
        """Gate on volatility regime from morning IV behavior.

        vol_spike_ratio tells us volume vs 20-day average (liquidity context).
        IV velocity (CE/PE IV trends) tells us if IV is compressing (good for long
        premium) or expanding (bad for long premium).
        """
        vol_spike = snap.vel("vol_spike_ratio")
        if vol_spike is None or math.isnan(vol_spike):
            return "PASS:no_vol_spike_data", 0.0

        # Volume context: high spike = good liquidity, low spike = sparse
        vol_score = 0.0
        if vol_spike >= 1.20:
            vol_score = 0.08
            vol_result = f"PASS:volume_elevated vol_spike={vol_spike:.2f}"
        elif vol_spike >= 0.85:
            vol_score = 0.0
            vol_result = f"PASS:volume_normal vol_spike={vol_spike:.2f}"
        else:
            vol_score = -0.10
            vol_result = f"WARN:volume_sparse vol_spike={vol_spike:.2f}"

        # IV compression: rate of CE IV change (compression = negative rate = good for premium)
        iv_compression_rate = snap.vel("vel_iv_compression_rate")
        if iv_compression_rate is not None and not math.isnan(iv_compression_rate):
            if iv_compression_rate < -0.001:  # IV compressing (negative = good)
                vol_score += 0.05
                vol_result += " + iv_compressing"
            elif iv_compression_rate > 0.002:  # IV expanding (positive = bad)
                vol_score -= 0.08
                vol_result = f"BLOCK:{vol_result[5:]} iv_expanding"

        return vol_result, vol_score
