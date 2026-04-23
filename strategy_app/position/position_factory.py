"""Builds a PositionContext from a TradeSignal and optional snapshot data.

Extracted from PositionTracker to separate position-state creation from
position-state management.
"""

from __future__ import annotations

import uuid
from typing import Optional

from ..contracts import PositionContext, TradeSignal
from ..engines.snapshot_accessor import SnapshotAccessor
from ..logging.decision_field_resolver import DecisionFieldResolver
from .trailing_manager import TrailingStopManager


class PositionFactory:
    """Creates PositionContext instances from entry signals."""

    def __init__(self) -> None:
        self._resolver = DecisionFieldResolver()

    def build(
        self,
        signal: TradeSignal,
        snap: SnapshotAccessor,
    ) -> PositionContext:
        """Validate signal data and construct a fully-populated PositionContext."""
        selected_strike = signal.strike or snap.atm_strike
        if selected_strike is None or int(selected_strike) <= 0:
            raise RuntimeError("cannot open position without a valid strike")
        selected_strike = int(selected_strike)

        premium = signal.entry_premium
        if premium is None or premium <= 0:
            premium = snap.option_ltp(signal.direction or "", selected_strike)
        if premium is None or premium <= 0:
            raise RuntimeError("cannot open position without a valid premium")

        engine_mode = self._resolver.effective_engine_mode(signal.engine_mode, source=signal.source)
        decision_mode = self._resolver.resolve_decision_mode_for_signal(signal, engine_mode)

        entry_futures_price = snap.fut_close
        underlying_stop_pct = float(signal.underlying_stop_pct) if signal.underlying_stop_pct is not None else None
        underlying_target_pct = float(signal.underlying_target_pct) if signal.underlying_target_pct is not None else None
        stop_price = TrailingStopManager._hard_stop_price(premium, float(signal.stop_loss_pct))

        return PositionContext(
            position_id=str(uuid.uuid4())[:8],
            direction=signal.direction or "",
            strike=selected_strike,
            expiry=signal.expiry,
            entry_premium=premium,
            entry_time=signal.timestamp,
            entry_snapshot_id=signal.snapshot_id,
            signal_id=str(signal.signal_id or "").strip() or None,
            lots=max(1, int(signal.max_lots or 1)),
            max_hold_bars=(max(1, int(signal.max_hold_bars)) if signal.max_hold_bars is not None else None),
            current_premium=premium,
            stop_loss_pct=float(signal.stop_loss_pct),
            stop_price=stop_price,
            entry_futures_price=entry_futures_price,
            underlying_stop_pct=underlying_stop_pct,
            underlying_target_pct=underlying_target_pct,
            high_water_premium=premium,
            target_pct=float(signal.target_pct),
            trailing_enabled=bool(signal.trailing_enabled),
            trailing_activation_pct=float(signal.trailing_activation_pct),
            trailing_offset_pct=float(signal.trailing_offset_pct),
            trailing_lock_breakeven=bool(signal.trailing_lock_breakeven),
            trailing_active=False,
            orb_trail_activation_mfe=float(signal.orb_trail_activation_mfe),
            orb_trail_offset_pct=float(signal.orb_trail_offset_pct),
            orb_trail_min_lock_pct=float(signal.orb_trail_min_lock_pct),
            orb_trail_priority_over_regime=bool(signal.orb_trail_priority_over_regime),
            orb_trail_regime_filter=(str(signal.orb_trail_regime_filter or "").strip().upper() or None),
            orb_trail_active=False,
            orb_trail_stop_price=None,
            oi_trail_activation_mfe=float(signal.oi_trail_activation_mfe),
            oi_trail_offset_pct=float(signal.oi_trail_offset_pct),
            oi_trail_min_lock_pct=float(signal.oi_trail_min_lock_pct),
            oi_trail_priority_over_regime=bool(signal.oi_trail_priority_over_regime),
            oi_trail_regime_filter=(str(signal.oi_trail_regime_filter or "").strip().upper() or None),
            oi_trail_active=False,
            oi_trail_stop_price=None,
            entry_strategy=str(signal.entry_strategy_name or ""),
            entry_regime=str(signal.entry_regime_name or ""),
            entry_reason=signal.reason,
            decision_metrics=self._resolver.signal_decision_metrics(signal),
            engine_mode=engine_mode,
            decision_mode=decision_mode,
            decision_reason_code=self._resolver.resolve_reason_code_for_signal(signal),
            strategy_family_version=self._resolver.resolve_strategy_family_version(
                explicit=signal.strategy_family_version,
                engine_mode=engine_mode,
                decision_mode=decision_mode,
            ),
            strategy_profile_id=self._resolver.resolve_strategy_profile_id(
                explicit=signal.strategy_profile_id,
                engine_mode=engine_mode,
            ),
        )
