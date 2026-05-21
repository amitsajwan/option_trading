"""Shared context objects that flow through the TradingBrain.

DayContext is the brain's "understanding" of the current trading day.
It is assembled once at morning_briefing time and carried through the
entire session.  Every gate, fitness evaluator, and provider reads from it.

SessionCarry captures state that persists across calendar days so the
brain can behave conservatively after a string of losses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional


class DayScore(str, Enum):
    """Macro-regime assessment for the day.

    CALM      — calm bullish/bearish drift; premium-selling strategies work.
    NEUTRAL   — no strong macro signal; normal intraday rules apply.
    VOLATILE  — elevated macro vol; reduce size, tighten stops.
    AVOID     — hard stop; no new entries regardless of intraday signal.
    UNKNOWN   — no daily context data available; fall back to intraday only.
    """

    CALM = "CALM"
    NEUTRAL = "NEUTRAL"
    VOLATILE = "VOLATILE"
    AVOID = "AVOID"
    UNKNOWN = "UNKNOWN"


@dataclass
class SessionCarry:
    """State carried over from the previous session.

    Populated by SessionMemory.load_carry() at morning_briefing time.
    Zero-values are safe defaults when no history exists.
    """

    consecutive_losses_at_close: int = 0
    prior_day_pnl_pct: float = 0.0
    prior_week_pnl_pct: float = 0.0
    losing_streak_days: int = 0
    last_trade_date: Optional[date] = None

    @classmethod
    def empty(cls) -> "SessionCarry":
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return {
            "consecutive_losses_at_close": self.consecutive_losses_at_close,
            "prior_day_pnl_pct": self.prior_day_pnl_pct,
            "prior_week_pnl_pct": self.prior_week_pnl_pct,
            "losing_streak_days": self.losing_streak_days,
            "last_trade_date": (
                self.last_trade_date.isoformat() if self.last_trade_date else None
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SessionCarry":
        last_date = None
        raw_date = raw.get("last_trade_date")
        if raw_date:
            try:
                last_date = date.fromisoformat(str(raw_date))
            except (ValueError, TypeError):
                pass
        return cls(
            consecutive_losses_at_close=int(raw.get("consecutive_losses_at_close", 0)),
            prior_day_pnl_pct=float(raw.get("prior_day_pnl_pct", 0.0)),
            prior_week_pnl_pct=float(raw.get("prior_week_pnl_pct", 0.0)),
            losing_streak_days=int(raw.get("losing_streak_days", 0)),
            last_trade_date=last_date,
        )


@dataclass
class FitnessScore:
    """How well a strategy fits today's context.

    Returned by StrategyPlugin.fits() and used by StrategyFitnessEvaluator
    to prune the active strategy set.
    """

    strategy_name: str
    fits: bool
    score: float = 1.0
    reasons: tuple[str, ...] = field(default_factory=tuple)
    size_multiplier: float = 1.0

    def __post_init__(self) -> None:
        self.score = max(0.0, min(1.0, float(self.score)))
        self.size_multiplier = max(0.0, min(1.0, float(self.size_multiplier)))


@dataclass
class DayContext:
    """The brain's full understanding of today's trading environment.

    Assembled once at morning_briefing time from all registered
    ContextProviders.  Passed to every gate and fitness evaluator.

    Daily regime features (regime_rv20, etc.) are None when the daily
    feature builder has not run yet — the brain degrades gracefully to
    DayScore.UNKNOWN in that case.
    """

    trade_date: date
    day_score: DayScore = DayScore.UNKNOWN
    day_score_confidence: float = 0.0
    day_score_reason: str = "no_context"

    # Daily macro features (from build_daily_regime_features.py)
    regime_rv20: Optional[float] = None        # trailing 20-day realized vol
    regime_dist_sma20: Optional[float] = None  # (spot - SMA20) / SMA20
    regime_sma20_slope: Optional[float] = None # 5-day slope of SMA20 (normalised)
    regime_60d_return: Optional[float] = None  # 60-day futures cumulative return
    vix_level: Optional[float] = None          # absolute India VIX (optional)

    # Raw key-value contributions from all ContextProviders
    provider_context: dict[str, Any] = field(default_factory=dict)

    # State carried over from the previous session
    session_carry: SessionCarry = field(default_factory=SessionCarry.empty)

    # Size multiplier override from fitness evaluation (1.0 = normal)
    size_multiplier: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "day_score": self.day_score.value,
            "day_score_confidence": round(self.day_score_confidence, 4),
            "day_score_reason": self.day_score_reason,
            "regime_rv20": self.regime_rv20,
            "regime_dist_sma20": self.regime_dist_sma20,
            "regime_sma20_slope": self.regime_sma20_slope,
            "regime_60d_return": self.regime_60d_return,
            "vix_level": self.vix_level,
            "size_multiplier": round(self.size_multiplier, 4),
            "session_carry": self.session_carry.to_dict(),
            "provider_context": self.provider_context,
        }


__all__ = [
    "DayContext",
    "DayScore",
    "FitnessScore",
    "SessionCarry",
]
