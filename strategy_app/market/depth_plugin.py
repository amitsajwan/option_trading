"""Concrete DepthPlugin implementations.

Depth acts as a confidence modifier, not a simple pass/fail gate:

  CE trade + strong CE bid  →  confidence_delta > 0  (depth aligned)
  CE trade + heavy ask pressure  →  confidence_delta < 0  (depth disagrees)
  PE trade + strong PE bid  →  confidence_delta > 0
  PE trade + heavy CE buying  →  confidence_delta < 0

The output ``confidence`` is the upstream direction confidence clamped to [0,1]
after applying the delta.  Downstream stages (Strike, Risk) can use this to
size positions or gate entries.

Gate behaviour (env vars):
  DEPTH_HARD_GATE=1       Block when depth strongly opposes direction.
  DEPTH_HARD_GATE=0       Advisory only — never blocks (default).
  DEPTH_MAX_SPREAD_PCT    Spread threshold; wider = poor liquidity (default 0.02).
  DEPTH_ALIGN_BOOST       Confidence boost when depth aligns (default +0.05).
  DEPTH_OPPOSE_PENALTY    Confidence reduction when depth opposes (default -0.10).
  DEPTH_HARD_BLOCK_THRESHOLD  Minimum confidence after penalty to hard-gate (default 0.30).

PassthroughDepthPlugin
  Default for replay / paper-trading — proceed=True, confidence unchanged.

LiveDepthPlugin
  Live mode — reads from RedisDepthReader, computes bid strength and confidence delta.

resolve_depth_plugin()
  Factory: auto-detects from DEPTH_FEED_ENABLED env var.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ..brain.plugin import DepthDecisionResult, DepthPlugin
from ..market.depth_context import StrikeDepth
from ..runtime.redis_depth_reader import RedisDepthReader, build_depth_reader_from_env

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bid_strength(depth: Optional[StrikeDepth]) -> Optional[float]:
    """Compute bid-side fraction: bid_qty / (bid_qty + ask_qty).  Range 0–1."""
    if depth is None or depth.bid_qty is None or depth.ask_qty is None:
        return None
    total = (depth.bid_qty or 0) + (depth.ask_qty or 0)
    if total <= 0:
        return None
    return float(depth.bid_qty) / float(total)


# ---------------------------------------------------------------------------
# Passthrough — default for replay / paper-trading
# ---------------------------------------------------------------------------


class PassthroughDepthPlugin(DepthPlugin):
    """Always proceeds without modifying confidence.

    Used in replay and paper-trading where no live depth feed runs.
    Satisfies the contract guarantee: depth absence never blocks a trade.
    """

    @property
    def plugin_id(self) -> str:
        return "passthrough_depth_v1"

    @property
    def plugin_version(self) -> str:
        return "1.0"

    def evaluate(
        self,
        direction: str,
        snapshot: dict[str, Any],
        context: dict[str, Any],
    ) -> DepthDecisionResult:
        upstream_confidence = float(context.get("upstream_confidence") or 0.0)
        return DepthDecisionResult(
            proceed=True,
            skip_reason=None,
            confidence_delta=None,
            ce_bid_strength=None,
            pe_bid_strength=None,
            spread_pct=None,
            depth_aligned=False,
            depth_available=False,
            plugin_id=self.plugin_id,
            plugin_version=self.plugin_version,
        )


# ---------------------------------------------------------------------------
# Live — reads from RedisDepthReader
# ---------------------------------------------------------------------------


class LiveDepthPlugin(DepthPlugin):
    """Evaluates live ATM option depth and adjusts direction confidence.

    ce_bid_strength / pe_bid_strength are computed as bid_qty / (bid_qty + ask_qty)
    for each option side.  A value > 0.6 signals buying pressure; < 0.4 signals
    selling pressure.

    When depth aligns with direction:  upstream_confidence + DEPTH_ALIGN_BOOST
    When depth opposes direction:      upstream_confidence + DEPTH_OPPOSE_PENALTY (negative)
    When DEPTH_HARD_GATE=1 and adjusted_confidence < DEPTH_HARD_BLOCK_THRESHOLD: block.
    """

    @property
    def plugin_id(self) -> str:
        return "live_depth_v1"

    @property
    def plugin_version(self) -> str:
        return "1.0"

    def __init__(self, *, reader: Optional[RedisDepthReader] = None) -> None:
        self._reader = reader if reader is not None else RedisDepthReader()
        self._max_spread_pct = _env_float("DEPTH_MAX_SPREAD_PCT", 0.02)
        self._align_boost = _env_float("DEPTH_ALIGN_BOOST", 0.05)
        self._oppose_penalty = _env_float("DEPTH_OPPOSE_PENALTY", -0.10)
        self._hard_gate = str(os.getenv("DEPTH_HARD_GATE") or "0").strip().lower() in {"1", "true", "yes"}
        self._hard_block_threshold = _env_float("DEPTH_HARD_BLOCK_THRESHOLD", 0.30)

    def evaluate(
        self,
        direction: str,
        snapshot: dict[str, Any],
        context: dict[str, Any],
    ) -> DepthDecisionResult:
        upstream_confidence = float(context.get("upstream_confidence") or 0.0)

        depth_ctx = None
        try:
            depth_ctx = self._reader.read_depth()
        except Exception:
            logger.debug("depth reader failed", exc_info=True)

        if depth_ctx is None or not depth_ctx.is_available:
            return DepthDecisionResult(
                proceed=True,
                skip_reason=None,
                confidence_delta=None,
                ce_bid_strength=None,
                pe_bid_strength=None,
                spread_pct=None,
                depth_aligned=False,
                depth_available=False,
                plugin_id=self.plugin_id,
                plugin_version=self.plugin_version,
            )

        ce_strength = _bid_strength(depth_ctx.ce)
        pe_strength = _bid_strength(depth_ctx.pe)

        # Determine alignment: does depth agree with the trade direction?
        target_strength = ce_strength if direction == "CE" else pe_strength
        opposing_strength = pe_strength if direction == "CE" else ce_strength

        depth_aligned = (
            target_strength is not None and target_strength > 0.55
            and (opposing_strength is None or opposing_strength < 0.55)
        )
        depth_opposed = (
            opposing_strength is not None and opposing_strength > 0.65
            and (target_strength is None or target_strength < 0.45)
        )

        # Apply confidence delta
        if depth_aligned:
            confidence_delta = self._align_boost
        elif depth_opposed:
            confidence_delta = self._oppose_penalty
        else:
            confidence_delta = 0.0

        adjusted_confidence = max(0.0, min(1.0, upstream_confidence + confidence_delta))

        # Spread quality check
        target_side = depth_ctx.ce if direction == "CE" else depth_ctx.pe
        spread_pct = target_side.relative_spread if target_side is not None else None
        spread_too_wide = spread_pct is not None and spread_pct > self._max_spread_pct

        # Hard gate: block when depth strongly opposes and confidence too low
        if self._hard_gate and depth_opposed and (adjusted_confidence < self._hard_block_threshold or spread_too_wide):
            return DepthDecisionResult(
                proceed=False,
                skip_reason=f"DEPTH_GATE:opposed conf={adjusted_confidence:.2f} spread={spread_pct}",
                confidence_delta=confidence_delta,
                ce_bid_strength=ce_strength,
                pe_bid_strength=pe_strength,
                spread_pct=spread_pct,
                depth_aligned=False,
                depth_available=True,
                plugin_id=self.plugin_id,
                plugin_version=self.plugin_version,
            )

        return DepthDecisionResult(
            proceed=True,
            skip_reason=None,
            confidence_delta=confidence_delta,
            ce_bid_strength=ce_strength,
            pe_bid_strength=pe_strength,
            spread_pct=spread_pct,
            depth_aligned=depth_aligned,
            depth_available=True,
            plugin_id=self.plugin_id,
            plugin_version=self.plugin_version,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resolve_depth_plugin(plugin_name: Optional[str] = None) -> DepthPlugin:
    """Return the appropriate DepthPlugin for the current environment.

    Resolution order:
      1. ``plugin_name`` argument (``'live'`` or ``'passthrough'``)
      2. ``DEPTH_FEED_ENABLED`` env var (auto-detects)
      3. Default: PassthroughDepthPlugin
    """
    name = str(plugin_name or "").strip().lower()
    if not name:
        reader = build_depth_reader_from_env()
        if reader is not None:
            return LiveDepthPlugin(reader=reader)
        return PassthroughDepthPlugin()
    if name == "live":
        return LiveDepthPlugin()
    return PassthroughDepthPlugin()


__all__ = ["PassthroughDepthPlugin", "LiveDepthPlugin", "resolve_depth_plugin"]
