"""Portfolio level risk management for the deterministic engine."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

from contracts_app import current_instrument, known_instruments
from contracts_app import capital_pool

from ..contracts import PositionContext, RiskContext
from ..runtime.runtime_artifacts import resolve_runtime_artifact_paths
from ..market.snapshot_accessor import SnapshotAccessor

logger = logging.getLogger(__name__)

from ..constants import (
    DEFAULT_CAPITAL_ALLOCATED,
    DEFAULT_MAX_CONSECUTIVE_LOSSES,
    DEFAULT_MAX_DAILY_LOSS_PCT,
    DEFAULT_MAX_LOTS_PER_TRADE,
    DEFAULT_MAX_SESSION_TRADES,
    DEFAULT_RISK_PER_TRADE_PCT,
    RISK_PROFILE_AGGRESSIVE_SAFE_V1,
    resolve_lot_size,
)
from ..utils.env import env_bool, env_float, env_int

_RISK_PROFILE_PRESETS: dict[str, dict[str, float | int | str]] = {
    RISK_PROFILE_AGGRESSIVE_SAFE_V1: {
        "RISK_LOT_SIZING_MODE": "budget_per_trade",
        "RISK_NOTIONAL_PER_TRADE": 50000.0,
        "RISK_LOT_BUDGET_USES_LOT_SIZE": 1,
        "RISK_CONFIDENCE_FLOOR": 0.65,
        "RISK_MAX_DAILY_LOSS_PCT": DEFAULT_MAX_DAILY_LOSS_PCT,
        "RISK_MAX_SESSION_TRADES": DEFAULT_MAX_SESSION_TRADES,
        "RISK_MAX_CONSECUTIVE_LOSSES": DEFAULT_MAX_CONSECUTIVE_LOSSES,
        "RISK_MAX_LOTS_PER_TRADE": 20,
        "RISK_PER_TRADE_PCT": DEFAULT_RISK_PER_TRADE_PCT,
        "RISK_CAPITAL_ALLOCATED": DEFAULT_CAPITAL_ALLOCATED,
        "RISK_VIX_HALT_THRESHOLD": 15.0,
        "RISK_VIX_RESUME_THRESHOLD": 8.0,
    }
}


class RiskManager:
    """Maintains session risk state and kill-switches."""

    def __init__(self) -> None:
        self._context = RiskContext()
        self._trade_date: Optional[date] = None
        self._operator_halt_path = resolve_runtime_artifact_paths().operator_halt_path
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
        # Shared capital pool (2026-09-21) -- off by default. See
        # contracts_app/capital_pool.py for the atomic reserve/release
        # primitive this wraps. `_active_reservation_amount` tracks the ONE
        # reservation this process can have outstanding, matching
        # PositionTracker's one-position-per-instrument invariant.
        self._pool_enabled = env_bool("CAPITAL_POOL_ENABLED", False)
        self._pool_total = env_float("CAPITAL_POOL_TOTAL_RS", 400_000.0) or 400_000.0
        self._pool_instrument_cap_pct = env_float("CAPITAL_POOL_MAX_INSTRUMENT_SHARE", 0.40) or 0.40
        self._active_reservation_amount: float = 0.0
        self._load_config()
        if self._pool_enabled:
            capital_pool.ensure_pool_document(self._pool_total, instruments=known_instruments())

    def _cfg_float(self, key: str, fallback: float) -> float:
        default = float(self._profile_defaults.get(key, fallback))
        value = env_float(key, default)
        return value if value is not None else default

    def _cfg_int(self, key: str, fallback: int) -> int:
        default = int(self._profile_defaults.get(key, fallback))
        value = env_int(key, default)
        return value if value is not None else default

    def _cfg_str(self, key: str, fallback: str) -> str:
        default = str(self._profile_defaults.get(key, fallback))
        return str(os.getenv(key, default) or default)

    def _load_config(self) -> None:
        self._context.max_daily_loss_pct = self._cfg_float("RISK_MAX_DAILY_LOSS_PCT", DEFAULT_MAX_DAILY_LOSS_PCT)
        self._context.max_session_trades = self._cfg_int("RISK_MAX_SESSION_TRADES", DEFAULT_MAX_SESSION_TRADES)
        self._context.max_consecutive_losses = self._cfg_int("RISK_MAX_CONSECUTIVE_LOSSES", DEFAULT_MAX_CONSECUTIVE_LOSSES)
        self._context.max_lots_per_trade = self._cfg_int("RISK_MAX_LOTS_PER_TRADE", DEFAULT_MAX_LOTS_PER_TRADE)
        self._context.risk_per_trade_pct = self._cfg_float("RISK_PER_TRADE_PCT", DEFAULT_RISK_PER_TRADE_PCT)
        self._context.capital_allocated = self._cfg_float("RISK_CAPITAL_ALLOCATED", DEFAULT_CAPITAL_ALLOCATED)
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
            self._operator_halt_path.exists()
            or self._context.daily_loss_breached
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
        if self._operator_halt_path.exists():
            return "operator_halt"
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
            unrealized_value = position.pnl_pct * position.entry_premium * position.lots * resolve_lot_size()
            ctx.session_unrealised_pnl = unrealized_value / ctx.capital_allocated
            ctx.capital_at_risk = position.entry_premium * position.lots * resolve_lot_size()
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

        # max_consecutive_losses <= 0 disables the pause (e.g. paper trading where we
        # want unlimited trades to observe behaviour) — mirrors the session-cap guard.
        if ctx.max_consecutive_losses > 0 and ctx.consecutive_losses >= ctx.max_consecutive_losses:
            if not ctx.consecutive_loss_limit:
                logger.warning("consecutive loss limit reached count=%d", ctx.consecutive_losses)
            ctx.consecutive_loss_limit = True

        self._check_vix_spike(snap)

    def record_trade_result(self, *, pnl_pct: float, lots: int = 1, entry_premium: float = 0.0) -> None:
        self.release_capital_reservation()
        ctx = self._context
        trade_pnl_value = pnl_pct * entry_premium * lots * resolve_lot_size()
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
                lot_cost *= resolve_lot_size()
            if lot_cost <= 0:
                return 1
            base_lots = int(self._notional_per_trade / lot_cost)
            scaled_lots = max(1, int(base_lots * confidence_scale))
            return min(scaled_lots, ctx.max_lots_per_trade)
        stop_pct = max(0.0, float(stop_loss_pct))
        if stop_pct <= 0:
            return 1
        risk_capital = ctx.capital_allocated * ctx.risk_per_trade_pct
        max_loss_per_lot = entry_premium * resolve_lot_size() * stop_pct
        if max_loss_per_lot <= 0:
            return 1
        base_lots = int(risk_capital / max_loss_per_lot)
        scaled = max(1, int(base_lots * confidence_scale))
        return min(scaled, ctx.max_lots_per_trade)

    def try_reserve_capital(self, *, lots: int, entry_premium: float, bypass: bool = False) -> tuple[bool, str]:
        """Reserve this instrument's share of the shared capital pool for an
        about-to-open position of `lots` lots at `entry_premium`.

        Returns (True, reason) when the caller may proceed unchanged;
        (False, reason) means the caller must block this entry, the same
        way an operator_halt block is handled today.

        `bypass=True` (sim/replay run ids) and CAPITAL_POOL_ENABLED=0 (the
        default) both short-circuit BEFORE `contracts_app.capital_pool` is
        ever imported-from-use -- this is the safety-by-default guarantee:
        with the flag off, this method never touches Mongo, never touches
        the shared pool, and always returns (True, ...), so behavior is
        byte-for-byte identical to before this feature existed.
        """
        if not self._pool_enabled:
            return True, "disabled"
        if bypass:
            return True, "sim_bypass"
        amount = float(entry_premium) * int(lots) * resolve_lot_size()
        if amount <= 0:
            return True, "zero_amount"
        cap = self._pool_total * self._pool_instrument_cap_pct
        ok, reason = capital_pool.try_reserve(current_instrument(), amount, per_instrument_cap=cap)
        if ok:
            self._active_reservation_amount = amount
        return ok, reason

    def release_capital_reservation(self) -> None:
        """Idempotent -- a no-op if the pool is disabled or nothing is
        currently reserved (e.g. this entry never called try_reserve_capital,
        or a previous release already cleared it)."""
        if not self._pool_enabled or self._active_reservation_amount <= 0:
            return
        capital_pool.release(current_instrument(), self._active_reservation_amount)
        self._active_reservation_amount = 0.0

    def live_eligible(self, *, grade: str, confidence: float = 1.0) -> tuple[bool, str]:
        """Whether an entry of this quality would be taken on REAL money.

        Paper mode takes everything (the caller does not block on this result) —
        this only labels the *tier*. The day we flip to live, the set of trades
        with tier=live is exactly the book that fires.

        Delegates the grade + recent-performance logic to
        signals.entry_quality.decide_tier (shared with the strategy path), then
        layers the manager-only operator-halt and confidence-floor checks.
        """
        # Local import avoids a circular import at module load (signals imports ml).
        from ..signals.entry_quality import decide_tier

        if self._operator_halt_path.exists():
            return False, "halted:operator_halt"
        decision = decide_tier(grade, self._context, confidence=confidence)
        if not decision.live_would_take:
            return False, decision.reason
        if float(confidence) < float(self._confidence_floor):
            return False, f"confidence_below_floor:{float(confidence):.2f}<{self._confidence_floor:.2f}"
        return True, decision.reason

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
