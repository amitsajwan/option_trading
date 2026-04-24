"""Single-position lifecycle tracking for deterministic strategies."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Optional

from ..contracts import ExitReason, PositionContext, RiskContext, SignalType, TradeSignal
from ..engines.snapshot_accessor import SnapshotAccessor
from .position_factory import PositionFactory

logger = logging.getLogger(__name__)

from ..constants import BANKNIFTY_LOT_SIZE, HARD_CLOSE_MINUTE, PRICE_EPS, SOFT_CLOSE_MINUTE
from .trailing_manager import TrailingStopManager


class PositionTracker:
    """Tracks a single open position and generates system exits."""

    def __init__(self) -> None:
        self._position: Optional[PositionContext] = None
        self._closed_positions: list[dict[str, object]] = []
        self._trailing_manager = TrailingStopManager()

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
        self._position = PositionFactory().build(signal, snap)
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
        self._trailing_manager.update(position)
        position.bars_held += 1

        current_futures_price = snap.fut_close

        exit_reason: Optional[ExitReason] = None
        exit_trigger = None
        if forced_exit_reason is not None:
            exit_reason = forced_exit_reason
            exit_trigger = "forced"
        elif risk.daily_loss_breached or risk.weekly_loss_breached:
            exit_reason = ExitReason.RISK_BREACH
            exit_trigger = "risk_breach"
        elif self._minute_of_day(snap) >= HARD_CLOSE_MINUTE:
            exit_reason = ExitReason.TIME_STOP
            exit_trigger = "hard_close"
        elif position.underlying_stop_pct is not None and self._is_underlying_stop_hit(position, current_futures_price):
            exit_reason = ExitReason.STOP_LOSS
            exit_trigger = "underlying_stop"
        elif self._is_stop_hit(position, current_premium):
            exit_reason = self._resolve_stop_exit_reason(position)
            exit_trigger = "premium_stop"
        elif position.underlying_target_pct is not None and self._is_underlying_target_hit(position, current_futures_price):
            exit_reason = ExitReason.TARGET_HIT
            exit_trigger = "underlying_target"
        elif position.target_pct > 0 and position.pnl_pct >= position.target_pct:
            exit_reason = ExitReason.TARGET_HIT
            exit_trigger = "premium_target"
        elif position.max_hold_bars is not None and position.bars_held >= int(position.max_hold_bars):
            exit_reason = ExitReason.TIME_STOP
            exit_trigger = "max_hold"
        elif self._minute_of_day(snap) >= SOFT_CLOSE_MINUTE:
            exit_reason = ExitReason.TIME_STOP
            exit_trigger = "soft_close"

        # INVESTIGATION LOG: Trace L6 exits
        if exit_reason is not None and position.underlying_stop_pct is not None:
            logger.warning(
                f"[TRACKER_EXIT_TRACE] exit_trigger={exit_trigger} exit_reason={exit_reason.value} "
                f"bars_held={position.bars_held} pnl_pct={position.pnl_pct:.4f} "
                f"underlying_stop_pct={position.underlying_stop_pct} entry_futures_price={position.entry_futures_price} "
                f"current_futures_price={current_futures_price} current_premium={current_premium} "
                f"stop_price={position.stop_price}"
            )

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

    def _is_stop_hit(self, position: PositionContext, current_premium: float) -> bool:
        stop_price = position.stop_price
        if stop_price is None or stop_price <= 0:
            return False
        return current_premium <= (stop_price + PRICE_EPS)

    def _resolve_stop_exit_reason(self, position: PositionContext) -> ExitReason:
        trail_reason = self._trailing_manager.resolve_exit_reason(position)
        if trail_reason == "TRAILING_STOP":
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
            "underlying_stop_pct": position.underlying_stop_pct,
            "underlying_target_pct": position.underlying_target_pct,
            "entry_futures_price": position.entry_futures_price,
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
