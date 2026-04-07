"""Portfolio level risk management for the deterministic engine."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

from ..contracts import PositionContext, RiskContext
from ..engines.snapshot_accessor import SnapshotAccessor

logger = logging.getLogger(__name__)

BANKNIFTY_LOT_SIZE = 15
RISK_PROFILE_AGGRESSIVE_SAFE_V1 = "aggressive_safe_v1"
_RISK_PROFILE_PRESETS: dict[str, dict[str, float | int | str]] = {
    RISK_PROFILE_AGGRESSIVE_SAFE_V1: {
        "RISK_LOT_SIZING_MODE": "budget_per_trade",
        "RISK_NOTIONAL_PER_TRADE": 50000.0,
        "RISK_LOT_BUDGET_USES_LOT_SIZE": 1,
        "RISK_CONFIDENCE_FLOOR": 0.65,
        "RISK_MAX_DAILY_LOSS_PCT": 0.02,
        "RISK_MAX_SESSION_TRADES": 6,
        "RISK_MAX_CONSECUTIVE_LOSSES": 3,
        "RISK_MAX_LOTS_PER_TRADE": 20,
        "RISK_PER_TRADE_PCT": 0.005,
        "RISK_CAPITAL_ALLOCATED": 500000.0,
        "RISK_VIX_HALT_THRESHOLD": 15.0,
        "RISK_VIX_RESUME_THRESHOLD": 8.0,
    }
}


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


class RiskManager:
    """Maintains session risk state and kill-switches."""

    def __init__(self) -> None:
        self._context = RiskContext()
        self._trade_date: Optional[date] = None
        self._risk_profile = str(os.getenv("RISK_PROFILE", "") or "").strip().lower()
        self._profile_defaults = dict(_RISK_PROFILE_PRESETS.get(self._risk_profile, {}))
        if self._risk_profile and not self._profile_defaults:
            logger.warning("unknown risk profile: %s", self._risk_profile)
        self._vix_halt_threshold = self._cfg_float("RISK_VIX_HALT_THRESHOLD", 15.0)
        self._vix_resume_threshold = self._cfg_float("RISK_VIX_RESUME_THRESHOLD", 8.0)
        self._vix_resume_cooldown = timedelta(minutes=30)
        self._lot_sizing_mode = "risk_based"
        self._notional_per_trade = 0.0
        self._lot_budget_uses_lot_size = True
        self._confidence_floor = 0.65
        self._load_config()

    def _cfg_float(self, key: str, fallback: float) -> float:
        default = float(self._profile_defaults.get(key, fallback))
        return _env_float(key, default)

    def _cfg_int(self, key: str, fallback: int) -> int:
        default = int(self._profile_defaults.get(key, fallback))
        return _env_int(key, default)

    def _cfg_str(self, key: str, fallback: str) -> str:
        default = str(self._profile_defaults.get(key, fallback))
        return str(os.getenv(key, default) or default)

    def _load_config(self) -> None:
        self._context.max_daily_loss_pct = self._cfg_float("RISK_MAX_DAILY_LOSS_PCT", 0.02)
        self._context.max_session_trades = self._cfg_int("RISK_MAX_SESSION_TRADES", 6)
        self._context.max_consecutive_losses = self._cfg_int("RISK_MAX_CONSECUTIVE_LOSSES", 3)
        self._context.max_lots_per_trade = self._cfg_int("RISK_MAX_LOTS_PER_TRADE", 5)
        self._context.risk_per_trade_pct = self._cfg_float("RISK_PER_TRADE_PCT", 0.005)
        self._context.capital_allocated = self._cfg_float("RISK_CAPITAL_ALLOCATED", 500000.0)
        self._lot_sizing_mode = self._cfg_str("RISK_LOT_SIZING_MODE", "risk_based").strip().lower()
        self._notional_per_trade = self._cfg_float("RISK_NOTIONAL_PER_TRADE", 0.0)
        self._lot_budget_uses_lot_size = self._cfg_int("RISK_LOT_BUDGET_USES_LOT_SIZE", 1) != 0
        self._confidence_floor = min(1.0, max(0.01, self._cfg_float("RISK_CONFIDENCE_FLOOR", 0.65)))

    def _confidence_scale(self, confidence: float) -> float:
        floor = min(1.0, max(0.01, float(self._confidence_floor)))
        clamped = min(1.0, max(floor, float(confidence)))
        return float(clamped)

    @property
    def context(self) -> RiskContext:
        return self._context

    @property
    def is_halted(self) -> bool:
        return bool(
            self._context.daily_loss_breached
            or self._context.session_trade_cap_breached
            or self._context.weekly_loss_breached
            or self._context.vix_spike_halt
        )

    @property
    def is_paused(self) -> bool:
        return bool(self._context.consecutive_loss_limit)

    @property
    def post_halt_resume_boost_available(self) -> bool:
        return bool(self._context.post_halt_resume_boost_available)

    @property
    def halt_reason(self) -> Optional[str]:
        ctx = self._context
        if ctx.daily_loss_breached:
            return "daily_loss_cap"
        if ctx.session_trade_cap_breached:
            return "session_trade_cap"
        if ctx.weekly_loss_breached:
            return "weekly_loss_cap"
        if ctx.vix_spike_halt:
            return "vix_spike_halt"
        return None

    @property
    def pause_reason(self) -> Optional[str]:
        if self._context.consecutive_loss_limit:
            return "consecutive_loss_pause"
        return None

    def consume_post_halt_resume_boost(self) -> bool:
        if not self._context.post_halt_resume_boost_available:
            return False
        self._context.post_halt_resume_boost_available = False
        return True

    def on_session_start(self, trade_date: date) -> None:
        self._trade_date = trade_date
        old = self._context
        self._context = RiskContext(
            capital_allocated=old.capital_allocated,
            max_daily_loss_pct=old.max_daily_loss_pct,
            max_session_trades=old.max_session_trades,
            max_consecutive_losses=old.max_consecutive_losses,
            max_lots_per_trade=old.max_lots_per_trade,
            risk_per_trade_pct=old.risk_per_trade_pct,
        )
        logger.info("risk manager session started: %s capital=%.0f", trade_date.isoformat(), self._context.capital_allocated)

    def on_session_end(self, trade_date: date) -> None:
        logger.info(
            "risk manager session ended: %s pnl=%.2f%% wins=%d losses=%d",
            trade_date.isoformat(),
            self._context.session_pnl_total * 100.0,
            self._context.session_win_count,
            self._context.session_loss_count,
        )

    def update(self, snap: SnapshotAccessor, position: Optional[PositionContext]) -> None:
        ctx = self._context
        if position is not None and ctx.capital_allocated > 0:
            unrealized_value = position.pnl_pct * position.entry_premium * position.lots * BANKNIFTY_LOT_SIZE
            ctx.session_unrealised_pnl = unrealized_value / ctx.capital_allocated
            ctx.capital_at_risk = position.entry_premium * position.lots * BANKNIFTY_LOT_SIZE
        else:
            ctx.session_unrealised_pnl = 0.0
            ctx.capital_at_risk = 0.0

        ctx.session_pnl_total = ctx.session_realised_pnl + ctx.session_unrealised_pnl

        if ctx.session_pnl_total < -ctx.max_daily_loss_pct:
            if not ctx.daily_loss_breached:
                logger.warning(
                    "daily loss breached pnl=%.2f%% limit=%.2f%%",
                    ctx.session_pnl_total * 100.0,
                    ctx.max_daily_loss_pct * 100.0,
                )
            ctx.daily_loss_breached = True

        if ctx.max_session_trades > 0 and ctx.session_trade_count >= ctx.max_session_trades:
            if not ctx.session_trade_cap_breached:
                logger.warning(
                    "session trade cap reached trades=%d limit=%d",
                    ctx.session_trade_count,
                    ctx.max_session_trades,
                )
            ctx.session_trade_cap_breached = True

        if ctx.consecutive_losses >= ctx.max_consecutive_losses:
            if not ctx.consecutive_loss_limit:
                logger.warning("consecutive loss limit reached count=%d", ctx.consecutive_losses)
            ctx.consecutive_loss_limit = True

        self._check_vix_spike(snap)

    def record_trade_result(self, *, pnl_pct: float, lots: int = 1, entry_premium: float = 0.0) -> None:
        ctx = self._context
        trade_pnl_value = pnl_pct * entry_premium * lots * BANKNIFTY_LOT_SIZE
        pnl_as_capital_pct = (trade_pnl_value / ctx.capital_allocated) if ctx.capital_allocated > 0 else 0.0
        ctx.session_trade_count += 1
        ctx.session_realised_pnl += pnl_as_capital_pct

        if pnl_pct > 0:
            ctx.session_win_count += 1
            ctx.consecutive_losses = 0
            ctx.consecutive_loss_limit = False
        else:
            ctx.session_loss_count += 1
            ctx.consecutive_losses += 1

        if ctx.max_session_trades > 0 and ctx.session_trade_count >= ctx.max_session_trades:
            ctx.session_trade_cap_breached = True

        logger.info(
            "trade result pnl=%.2f%% realized_session=%.2f%% wins=%d losses=%d consec=%d trades=%d",
            pnl_pct * 100.0,
            ctx.session_realised_pnl * 100.0,
            ctx.session_win_count,
            ctx.session_loss_count,
            ctx.consecutive_losses,
            ctx.session_trade_count,
        )

    def compute_lots(self, *, entry_premium: float, stop_loss_pct: float = 0.40, confidence: float = 1.0) -> int:
        ctx = self._context
        if entry_premium <= 0 or ctx.capital_allocated <= 0:
            return 1
        confidence_scale = self._confidence_scale(float(confidence))
        if self._lot_sizing_mode in {"budget_per_trade", "notional_budget"} and self._notional_per_trade > 0:
            lot_cost = float(entry_premium)
            if self._lot_budget_uses_lot_size:
                lot_cost *= BANKNIFTY_LOT_SIZE
            if lot_cost <= 0:
                return 1
            base_lots = int(self._notional_per_trade / lot_cost)
            scaled_lots = max(1, int(base_lots * confidence_scale))
            return min(scaled_lots, ctx.max_lots_per_trade)
        stop_pct = max(0.0, float(stop_loss_pct))
        if stop_pct <= 0:
            return 1
        risk_capital = ctx.capital_allocated * ctx.risk_per_trade_pct
        max_loss_per_lot = entry_premium * BANKNIFTY_LOT_SIZE * stop_pct
        if max_loss_per_lot <= 0:
            return 1
        base_lots = int(risk_capital / max_loss_per_lot)
        scaled = max(1, int(base_lots * confidence_scale))
        return min(scaled, ctx.max_lots_per_trade)

    def _check_vix_spike(self, snap: SnapshotAccessor) -> None:
        ctx = self._context
        vix_chg_pct = snap.vix_intraday_chg
        now_ts = snap.timestamp or snap.timestamp_or_now
        if vix_chg_pct is not None:
            abs_chg = abs(vix_chg_pct)
            if abs_chg >= self._vix_halt_threshold:
                if not ctx.vix_spike_halt:
                    logger.warning("vix halt triggered intraday_change=%.2f%%", vix_chg_pct)
                ctx.vix_spike_halt = True
                ctx.vix_last_halt_at = now_ts
                ctx.vix_below_resume_since = None
                ctx.post_halt_resume_boost_available = False
                return

            if ctx.vix_spike_halt:
                if abs_chg < self._vix_resume_threshold:
                    if ctx.vix_below_resume_since is None:
                        ctx.vix_below_resume_since = now_ts
                    elif (now_ts - ctx.vix_below_resume_since) >= self._vix_resume_cooldown:
                        logger.info(
                            "vix spike resolved intraday_change=%.2f%% cooldown_minutes=%d",
                            vix_chg_pct,
                            int(self._vix_resume_cooldown.total_seconds() // 60),
                        )
                        ctx.vix_spike_halt = False
                        ctx.vix_last_resume_at = now_ts
                        ctx.vix_below_resume_since = None
                        ctx.post_halt_resume_boost_available = True
                else:
                    ctx.vix_below_resume_since = None
            return

        if snap.vix_spike_flag:
            if not ctx.vix_spike_halt:
                logger.warning("vix halt triggered from snapshot flag")
            ctx.vix_spike_halt = True
            ctx.vix_last_halt_at = now_ts
            ctx.vix_below_resume_since = None
            ctx.post_halt_resume_boost_available = False
            return

        if ctx.vix_spike_halt:
            if ctx.vix_below_resume_since is None:
                ctx.vix_below_resume_since = now_ts
            elif (now_ts - ctx.vix_below_resume_since) >= self._vix_resume_cooldown:
                logger.info(
                    "vix spike resolved from missing chg payload cooldown_minutes=%d",
                    int(self._vix_resume_cooldown.total_seconds() // 60),
                )
                ctx.vix_spike_halt = False
                ctx.vix_last_resume_at = now_ts
                ctx.vix_below_resume_since = None
                ctx.post_halt_resume_boost_available = True
            else:
                # wait through cooldown while intraday data is missing
                pass
        else:
            ctx.vix_below_resume_since = None
