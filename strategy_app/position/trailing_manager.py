"""Trailing-stop logic extracted from PositionTracker.

Three flavours are supported:
1. Generic tiered trailing (activation at +15 % MFE, offset shrinks as MFE grows).
2. ORB premium trail (strategy-specific, with regime filter and min-lock).
3. OI_BUILDUP premium trail (strategy-specific, same mechanics as ORB).

All write into PositionContext.stop_price so the tracker remains the single
source of truth for whether a stop is hit.
"""

from __future__ import annotations

from typing import Optional

from ..contracts import PositionContext
from ..constants import PRICE_EPS


class TrailingStopManager:
    """Manages trailing-stop price updates for an open position."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def update(self, position: PositionContext) -> None:
        """Evaluate all three trailing flavours and move stop_price upward."""
        self._apply_generic_trail(position)
        self._apply_orb_trail(position)
        self._apply_oi_trail(position)

    def resolve_exit_reason(self, position: PositionContext) -> Optional[str]:
        """Return 'TRAILING_STOP' if any trail flavour is active and the
        current stop_price sits above the hard stop.  None otherwise."""
        if not any((position.trailing_active, position.orb_trail_active, position.oi_trail_active)):
            return None
        if position.stop_price is None:
            return None
        hard_stop = self._hard_stop_price(position.entry_premium, position.stop_loss_pct)
        if hard_stop is None:
            return None
        if position.stop_price > (hard_stop + PRICE_EPS):
            return "TRAILING_STOP"
        return None

    # ------------------------------------------------------------------
    # Generic tiered trail
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_generic_trail(position: PositionContext) -> None:
        if not position.trailing_enabled or position.entry_premium <= 0:
            return
        offset = _tiered_trailing_offset(position.mfe_pct)
        if offset is None:
            return
        position.trailing_active = True
        candidate = position.high_water_premium * (1.0 - offset)
        if position.trailing_lock_breakeven:
            candidate = max(candidate, position.entry_premium)
        if position.stop_price is None or candidate > position.stop_price:
            position.stop_price = candidate

    # ------------------------------------------------------------------
    # ORB trail
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_orb_trail(position: PositionContext) -> None:
        if position.entry_strategy != "ORB" or position.entry_premium <= 0:
            return
        if position.orb_trail_regime_filter is not None and str(position.entry_regime).upper() != position.orb_trail_regime_filter:
            return
        if position.orb_trail_offset_pct <= 0:
            return
        if position.mfe_pct + PRICE_EPS < max(0.0, position.orb_trail_activation_mfe):
            return
        position.orb_trail_active = True
        trail_stop = position.high_water_premium * (1.0 - max(0.0, position.orb_trail_offset_pct))
        min_lock_stop = position.entry_premium * (1.0 + max(0.0, position.orb_trail_min_lock_pct))
        candidate = max(trail_stop, min_lock_stop)
        if position.orb_trail_stop_price is None or candidate > position.orb_trail_stop_price:
            position.orb_trail_stop_price = candidate
        if position.stop_price is None or candidate > position.stop_price:
            position.stop_price = candidate

    # ------------------------------------------------------------------
    # OI_BUILDUP trail
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_oi_trail(position: PositionContext) -> None:
        if position.entry_strategy != "OI_BUILDUP" or position.entry_premium <= 0:
            return
        if position.oi_trail_regime_filter is not None and str(position.entry_regime).upper() != position.oi_trail_regime_filter:
            return
        if position.oi_trail_offset_pct <= 0:
            return
        if position.mfe_pct + PRICE_EPS < max(0.0, position.oi_trail_activation_mfe):
            return
        position.oi_trail_active = True
        trail_stop = position.high_water_premium * (1.0 - max(0.0, position.oi_trail_offset_pct))
        min_lock_stop = position.entry_premium * (1.0 + max(0.0, position.oi_trail_min_lock_pct))
        candidate = max(trail_stop, min_lock_stop)
        if position.oi_trail_stop_price is None or candidate > position.oi_trail_stop_price:
            position.oi_trail_stop_price = candidate
        if position.stop_price is None or candidate > position.stop_price:
            position.stop_price = candidate

    @staticmethod
    def _hard_stop_price(entry_premium: float, stop_loss_pct: float) -> Optional[float]:
        if entry_premium <= 0 or stop_loss_pct <= 0:
            return None
        return entry_premium * (1.0 - stop_loss_pct)


def _tiered_trailing_offset(mfe_pct: float) -> Optional[float]:
    mfe = max(0.0, float(mfe_pct))
    if mfe < 0.15:
        return None
    if mfe < 0.25:
        return 0.07
    if mfe < 0.40:
        return 0.05
    return 0.03
