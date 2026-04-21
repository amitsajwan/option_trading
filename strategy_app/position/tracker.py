"""Single-position lifecycle tracking for deterministic strategies."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Optional

from ..contracts import ExitReason, PositionContext, RiskContext, SignalType, TradeSignal
from ..engines.snapshot_accessor import SnapshotAccessor
from ..logging.decision_field_resolver import DecisionFieldResolver

logger = logging.getLogger(__name__)

BANKNIFTY_LOT_SIZE = 15
SOFT_CLOSE_MINUTE = 15 * 60
HARD_CLOSE_MINUTE = 15 * 60 + 15
PRICE_EPS = 1e-9


class PositionTracker:
    """Tracks a single open position and generates system exits."""

    def __init__(self) -> None:
        self._position: Optional[PositionContext] = None
        self._closed_positions: list[dict[str, object]] = []
        self._resolver = DecisionFieldResolver()

    def on_session_start(self, trade_date: date) -> None:
        self._position = None
        self._closed_positions = []
        logger.info("position tracker session started: %s", trade_date.isoformat())

    def on_session_end(self, trade_date: date) -> None:
        if self._position is not None:
            logger.warning("session ended with open position id=%s", self._position.position_id)
        logger.info("position tracker session ended: %s closed=%d", trade_date.isoformat(), len(self._closed_positions))

    @property
    def has_position(self) -> bool:
        return self._position is not None

    @property
    def current_position(self) -> Optional[PositionContext]:
        return self._position

    def open_position(self, signal: TradeSignal, snap: SnapshotAccessor) -> PositionContext:
        if self._position is not None:
            raise RuntimeError(f"position already open: {self._position.position_id}")

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
        premium_stop_pct = float(signal.stop_loss_pct)
        stop_price = (
            None
            if underlying_stop_pct is not None
            else self._hard_stop_price(premium, premium_stop_pct)
        )
        self._position = PositionContext(
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
        logger.info(
            "position opened id=%s dir=%s strike=%s premium=%.2f lots=%d stop=%.2f trailing=%s",
            self._position.position_id,
            self._position.direction,
            self._position.strike,
            self._position.entry_premium,
            self._position.lots,
            self._position.stop_price or 0.0,
            self._position.trailing_enabled,
        )
        return self._position

    def update(
        self,
        snap: SnapshotAccessor,
        risk: RiskContext,
        forced_exit_reason: Optional[ExitReason] = None,
    ) -> Optional[TradeSignal]:
        if self._position is None:
            return None

        position = self._position
        current_premium = self._current_premium(snap, position.direction, position.strike)
        if (current_premium is None or current_premium <= 0) and forced_exit_reason is None:
            position.bars_held += 1
            return None
        if current_premium is None or current_premium <= 0:
            current_premium = position.current_premium if position.current_premium > 0 else position.entry_premium

        position.current_premium = current_premium
        position.pnl_pct = (current_premium - position.entry_premium) / position.entry_premium
        position.mfe_pct = max(position.mfe_pct, position.pnl_pct)
        position.mae_pct = min(position.mae_pct, position.pnl_pct)
        position.high_water_premium = max(position.high_water_premium, current_premium)
        self._apply_trailing_stop(position)
        self._apply_orb_premium_trail(position)
        self._apply_oi_premium_trail(position)
        position.bars_held += 1

        current_futures_price = snap.fut_close

        exit_reason: Optional[ExitReason] = None
        if forced_exit_reason is not None:
            exit_reason = forced_exit_reason
        elif risk.daily_loss_breached or risk.weekly_loss_breached:
            exit_reason = ExitReason.RISK_BREACH
        elif self._minute_of_day(snap) >= HARD_CLOSE_MINUTE:
            exit_reason = ExitReason.TIME_STOP
        elif position.underlying_stop_pct is not None and self._is_underlying_stop_hit(position, current_futures_price):
            exit_reason = ExitReason.STOP_LOSS
        elif position.underlying_stop_pct is None and self._is_stop_hit(position, current_premium):
            exit_reason = self._resolve_stop_exit_reason(position)
        elif position.underlying_target_pct is not None and self._is_underlying_target_hit(position, current_futures_price):
            exit_reason = ExitReason.TARGET_HIT
        elif position.underlying_target_pct is None and position.pnl_pct >= position.target_pct:
            exit_reason = ExitReason.TARGET_HIT
        elif position.max_hold_bars is not None and position.bars_held >= int(position.max_hold_bars):
            exit_reason = ExitReason.TIME_STOP
        elif self._minute_of_day(snap) >= SOFT_CLOSE_MINUTE:
            exit_reason = ExitReason.TIME_STOP

        if exit_reason is None:
            return None
        return self._close_position(snap, exit_reason, current_premium)

    def force_exit(self, snap: SnapshotAccessor, reason: ExitReason) -> Optional[TradeSignal]:
        return self.update(snap, RiskContext(), forced_exit_reason=reason)

    def session_stats(self) -> dict[str, object]:
        if not self._closed_positions:
            return {"trades": 0}
        pnls = [float(item["pnl_pct"]) for item in self._closed_positions]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value <= 0]
        return {
            "trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(pnls)) if pnls else 0.0,
            "avg_pnl_pct": sum(pnls) / len(pnls),
            "avg_mfe_pct": sum(float(item["mfe_pct"]) for item in self._closed_positions) / len(self._closed_positions),
            "avg_mae_pct": sum(float(item["mae_pct"]) for item in self._closed_positions) / len(self._closed_positions),
        }

    def _current_premium(self, snap: SnapshotAccessor, direction: str, strike: int) -> Optional[float]:
        return snap.option_ltp(direction, strike)

    def _minute_of_day(self, snap: SnapshotAccessor) -> int:
        ts = snap.timestamp
        if ts is None:
            return 0
        return ts.hour * 60 + ts.minute

    def _is_underlying_stop_hit(self, position: PositionContext, current_futures_price: Optional[float]) -> bool:
        if current_futures_price is None or current_futures_price <= 0:
            return False
        if position.entry_futures_price is None or position.entry_futures_price <= 0:
            return False
        stop_pct = float(position.underlying_stop_pct or 0.0)
        if stop_pct <= 0:
            return False
        if position.direction == "CE":
            return current_futures_price <= position.entry_futures_price * (1.0 - stop_pct)
        if position.direction == "PE":
            return current_futures_price >= position.entry_futures_price * (1.0 + stop_pct)
        return False

    def _is_underlying_target_hit(self, position: PositionContext, current_futures_price: Optional[float]) -> bool:
        if current_futures_price is None or current_futures_price <= 0:
            return False
        if position.entry_futures_price is None or position.entry_futures_price <= 0:
            return False
        target_pct = float(position.underlying_target_pct or 0.0)
        if target_pct <= 0:
            return False
        if position.direction == "CE":
            return current_futures_price >= position.entry_futures_price * (1.0 + target_pct)
        if position.direction == "PE":
            return current_futures_price <= position.entry_futures_price * (1.0 - target_pct)
        return False

    def _hard_stop_price(self, entry_premium: float, stop_loss_pct: float) -> Optional[float]:
        if entry_premium <= 0 or stop_loss_pct <= 0:
            return None
        return entry_premium * (1.0 - stop_loss_pct)

    def _apply_trailing_stop(self, position: PositionContext) -> None:
        if not position.trailing_enabled or position.entry_premium <= 0:
            return
        offset = self._tiered_trailing_offset(position.mfe_pct)
        if offset is None:
            return
        position.trailing_active = True
        candidate = position.high_water_premium * (1.0 - offset)
        if position.trailing_lock_breakeven:
            candidate = max(candidate, position.entry_premium)
        if position.stop_price is None or candidate > position.stop_price:
            position.stop_price = candidate

    def _tiered_trailing_offset(self, mfe_pct: float) -> Optional[float]:
        mfe = max(0.0, float(mfe_pct))
        if mfe < 0.15:
            return None
        if mfe < 0.25:
            return 0.07
        if mfe < 0.40:
            return 0.05
        return 0.03

    def _apply_orb_premium_trail(self, position: PositionContext) -> None:
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

    def _apply_oi_premium_trail(self, position: PositionContext) -> None:
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

    def _is_stop_hit(self, position: PositionContext, current_premium: float) -> bool:
        stop_price = position.stop_price
        if stop_price is None or stop_price <= 0:
            return False
        return current_premium <= (stop_price + PRICE_EPS)

    def _resolve_stop_exit_reason(self, position: PositionContext) -> ExitReason:
        hard_stop = self._hard_stop_price(position.entry_premium, position.stop_loss_pct)
        if (
            (position.trailing_active or position.orb_trail_active or position.oi_trail_active)
            and position.stop_price is not None
            and hard_stop is not None
            and position.stop_price > (hard_stop + PRICE_EPS)
        ):
            return ExitReason.TRAILING_STOP
        return ExitReason.STOP_LOSS

    def _close_position(self, snap: SnapshotAccessor, reason: ExitReason, exit_premium: float) -> TradeSignal:
        if self._position is None:
            raise RuntimeError("no open position to close")
        position = self._position
        timestamp = snap.timestamp_or_now
        closed_record = {
            "position_id": position.position_id,
            "direction": position.direction,
            "entry_premium": position.entry_premium,
            "exit_premium": exit_premium,
            "pnl_pct": position.pnl_pct,
            "mfe_pct": position.mfe_pct,
            "mae_pct": position.mae_pct,
            "bars_held": position.bars_held,
            "max_hold_bars": position.max_hold_bars,
            "stop_loss_pct": position.stop_loss_pct,
            "stop_price": position.stop_price,
            "high_water_premium": position.high_water_premium,
            "target_pct": position.target_pct,
            "trailing_enabled": position.trailing_enabled,
            "trailing_activation_pct": position.trailing_activation_pct,
            "trailing_offset_pct": position.trailing_offset_pct,
            "trailing_lock_breakeven": position.trailing_lock_breakeven,
            "trailing_active": position.trailing_active,
            "orb_trail_activation_mfe": position.orb_trail_activation_mfe,
            "orb_trail_offset_pct": position.orb_trail_offset_pct,
            "orb_trail_min_lock_pct": position.orb_trail_min_lock_pct,
            "orb_trail_priority_over_regime": position.orb_trail_priority_over_regime,
            "orb_trail_regime_filter": position.orb_trail_regime_filter,
            "orb_trail_active": position.orb_trail_active,
            "orb_trail_stop_price": position.orb_trail_stop_price,
            "oi_trail_activation_mfe": position.oi_trail_activation_mfe,
            "oi_trail_offset_pct": position.oi_trail_offset_pct,
            "oi_trail_min_lock_pct": position.oi_trail_min_lock_pct,
            "oi_trail_priority_over_regime": position.oi_trail_priority_over_regime,
            "oi_trail_regime_filter": position.oi_trail_regime_filter,
            "oi_trail_active": position.oi_trail_active,
            "oi_trail_stop_price": position.oi_trail_stop_price,
            "exit_reason": reason.value,
            "entry_time": position.entry_time.isoformat(),
            "exit_time": timestamp.isoformat(),
            "entry_strategy": position.entry_strategy,
        }
        self._closed_positions.append(closed_record)

        signal = TradeSignal(
            signal_id=str(uuid.uuid4())[:8],
            timestamp=timestamp,
            snapshot_id=snap.snapshot_id,
            signal_type=SignalType.EXIT,
            direction=position.direction,
            strike=position.strike,
            entry_premium=position.entry_premium,
            position_id=position.position_id,
            exit_reason=reason,
            reason=(
                f"{reason.value} pnl={position.pnl_pct:.2%} mfe={position.mfe_pct:.2%} "
                f"mae={position.mae_pct:.2%} stop={position.stop_price or 0.0:.2f}"
            ),
        )
        self._position = None
        logger.info("position closed id=%s reason=%s pnl=%.2f%%", signal.position_id, reason.value, position.pnl_pct * 100.0)
        return signal
