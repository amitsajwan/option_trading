"""TradingBrain — the intelligent coordinator.

The brain sits above DeterministicRuleEngine and provides three things
that the engine alone cannot do:

1.  **Morning context** — assembles all ContextProvider contributions into a
    DayContext before the session opens.  Includes daily macro features,
    LLM context (stub), and cross-session carry state.

2.  **Entry gate** — checks ConsensusGate (N-of-M agreement) and
    StrategyFitnessEvaluator (does any active strategy fit today?) before
    accepting an entry vote from the engine.

3.  **Session memory** — persists a summary at session end so the next day's
    morning briefing knows about yesterday's consecutive losses, P&L, etc.

Design principles
-----------------
* The brain never replaces the engine's logic — it wraps the entry path.
* Every extension point (providers, plugins, consensus) is pluggable.
* All state is observable: morning_briefing() returns the full DayContext dict
  which is logged so every decision can be replayed.
* Failure in any provider/plugin is isolated — the brain degrades gracefully.

Environment variables
---------------------
BRAIN_ENABLED (bool, default true)
    Set to false to disable the brain entirely (engine behaviour unchanged).
BRAIN_CONSENSUS_MIN_AGREEING (int, default 1)
    Minimum strategy agreement for entry.  1 = original behaviour.
BRAIN_CONSENSUS_REQUIRE_DIRECTION (bool, default false)
    Block entries when CE/PE votes conflict even if min_agreeing is met.
BRAIN_DAILY_FEATURES_PATH (str)
    Path to daily_regime_features.json (see DailyFeaturesProvider).
BRAIN_LLM_ENABLED (bool, default false)
    Enable LLM-powered morning briefing (see LLMContextProvider).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from ..contracts import StrategyVote
from .consensus import ConsensusGate, ConsensusResult
from .context import DayContext, DayScore, SessionCarry
from .fitness import StrategyFitnessEvaluator
from .plugin import ContextProvider
from .providers.daily_features import DailyFeaturesProvider
from .providers.llm_stub import LLMContextProvider
from .session_memory import SessionMemory

logger = logging.getLogger(__name__)

_DAY_SCORE_PRIORITY: dict[str, int] = {
    "AVOID": 4,
    "VOLATILE": 3,
    "NEUTRAL": 2,
    "CALM": 1,
    "UNKNOWN": 0,
}


@dataclass(frozen=True)
class BrainDecision:
    """Result of gate_entry()."""

    allowed: bool
    reason: str
    day_score: str = DayScore.UNKNOWN.value
    consensus: Optional[ConsensusResult] = None
    size_multiplier: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "allowed": self.allowed,
            "reason": self.reason,
            "day_score": self.day_score,
            "size_multiplier": round(self.size_multiplier, 4),
        }
        if self.consensus is not None:
            result["consensus"] = {
                "agreed_direction": self.consensus.agreed_direction,
                "agreeing_count": self.consensus.agreeing_count,
                "total_count": self.consensus.total_count,
                "reason": self.consensus.reason,
            }
        return result


class TradingBrain:
    """Intelligent coordinator wrapping the strategy engine entry path.

    Lifecycle
    ---------
    morning_briefing(trade_date) → DayContext   called once before session open
    gate_entry(votes, context)  → BrainDecision called before each entry
    on_trade_result(pnl_pct, …)                 called after each position closes
    save_session_summary(trade_date)             called at session end
    """

    def __init__(
        self,
        *,
        context_providers: Optional[list[ContextProvider]] = None,
        fitness_evaluator: Optional[StrategyFitnessEvaluator] = None,
        consensus_gate: Optional[ConsensusGate] = None,
        session_memory: Optional[SessionMemory] = None,
        enabled: bool = True,
    ) -> None:
        self._enabled = bool(enabled)
        self._providers: list[ContextProvider] = list(context_providers or [])
        self._fitness = fitness_evaluator or StrategyFitnessEvaluator()
        self._consensus = consensus_gate or ConsensusGate.from_env()
        self._memory = session_memory or SessionMemory()

        # Session state (reset each morning)
        self._current_context: Optional[DayContext] = None
        self._session_trades: int = 0
        self._session_wins: int = 0
        self._session_losses: int = 0
        self._session_pnl_pct: float = 0.0
        self._consecutive_losses: int = 0

        logger.info(
            "trading_brain initialised enabled=%s providers=%s consensus_min=%d",
            self._enabled,
            [p.name for p in self._providers],
            self._consensus.min_agreeing,
        )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "TradingBrain":
        """Build a production brain from environment variables."""
        enabled = (
            os.getenv("BRAIN_ENABLED", "true").strip().lower()
            not in ("0", "false", "no")
        )
        providers: list[ContextProvider] = [
            DailyFeaturesProvider(),
            LLMContextProvider(),
        ]
        return cls(
            context_providers=providers,
            consensus_gate=ConsensusGate.from_env(),
            enabled=enabled,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current_context(self) -> Optional[DayContext]:
        return self._current_context

    def morning_briefing(self, trade_date: date) -> DayContext:
        """Assemble DayContext for *trade_date*.  Call once before session open.

        Steps:
        1.  Load cross-session carry from SessionMemory.
        2.  Call each ContextProvider and merge results.
        3.  Synthesise DayScore from provider outputs.
        4.  Reset intra-session counters.
        """
        self._reset_session_counters()

        carry = self._memory.load_carry(trade_date)
        provider_ctx: dict[str, Any] = {}
        for provider in self._providers:
            try:
                contribution = provider.provide(trade_date)
                if isinstance(contribution, dict):
                    provider_ctx.update(contribution)
            except Exception as exc:
                logger.warning(
                    "context_provider failed name=%s date=%s error=%s",
                    provider.name,
                    trade_date,
                    exc,
                )

        context = self._build_context(trade_date, carry, provider_ctx)
        self._current_context = context
        logger.info(
            "morning_briefing date=%s day_score=%s confidence=%.2f reason=%s "
            "carry_losses=%d size_mult=%.2f",
            trade_date,
            context.day_score.value,
            context.day_score_confidence,
            context.day_score_reason,
            carry.consecutive_losses_at_close,
            context.size_multiplier,
        )
        return context

    def gate_entry(
        self,
        entry_votes: list[StrategyVote],
        context: Optional[DayContext] = None,
    ) -> BrainDecision:
        """Evaluate entry votes against brain gates.  Returns allow/block.

        Gates (in order):
        1.  Brain disabled → always allow (passthrough).
        2.  DayScore.AVOID → block.
        3.  ConsensusGate → require N-of-M agreement.
        """
        ctx = context or self._current_context
        day_score_value = ctx.day_score.value if ctx else DayScore.UNKNOWN.value

        if not self._enabled:
            return BrainDecision(
                allowed=True,
                reason="brain_disabled",
                day_score=day_score_value,
                size_multiplier=1.0,
            )

        if ctx is not None and ctx.day_score == DayScore.AVOID:
            return BrainDecision(
                allowed=False,
                reason="day_score_avoid",
                day_score=day_score_value,
                size_multiplier=0.0,
            )

        consensus = self._consensus.evaluate(entry_votes)
        if not consensus.allowed:
            return BrainDecision(
                allowed=False,
                reason=f"consensus_gate:{consensus.reason}",
                day_score=day_score_value,
                consensus=consensus,
                size_multiplier=0.0,
            )

        size_mult = ctx.size_multiplier if ctx is not None else 1.0

        return BrainDecision(
            allowed=True,
            reason="brain_pass",
            day_score=day_score_value,
            consensus=consensus,
            size_multiplier=size_mult,
        )

    def on_trade_result(self, *, pnl_pct: float, strategy_name: str = "") -> None:
        """Update session counters after a position closes."""
        self._session_trades += 1
        self._session_pnl_pct += float(pnl_pct)
        if float(pnl_pct) > 0:
            self._session_wins += 1
            self._consecutive_losses = 0
        else:
            self._session_losses += 1
            self._consecutive_losses += 1
        logger.debug(
            "brain trade_result pnl=%.4f strategy=%s consec_losses=%d session_pnl=%.4f",
            pnl_pct,
            strategy_name,
            self._consecutive_losses,
            self._session_pnl_pct,
        )

    def save_session_summary(self, trade_date: date) -> None:
        """Persist session summary to JSONL memory.  Call at session end."""
        try:
            self._memory.save_summary(
                trade_date=trade_date,
                trades=self._session_trades,
                wins=self._session_wins,
                losses=self._session_losses,
                consecutive_losses=self._consecutive_losses,
                session_pnl_pct=self._session_pnl_pct,
            )
        except Exception as exc:
            logger.warning("brain session_summary save failed error=%s", exc)

    def context_summary(self) -> dict[str, Any]:
        """Return current context as a loggable dict."""
        if self._current_context is None:
            return {"brain_enabled": self._enabled, "context": None}
        return {
            "brain_enabled": self._enabled,
            "context": self._current_context.to_dict(),
            "session": {
                "trades": self._session_trades,
                "wins": self._session_wins,
                "losses": self._session_losses,
                "consecutive_losses": self._consecutive_losses,
                "session_pnl_pct": round(self._session_pnl_pct, 6),
            },
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reset_session_counters(self) -> None:
        self._session_trades = 0
        self._session_wins = 0
        self._session_losses = 0
        self._session_pnl_pct = 0.0
        self._consecutive_losses = 0
        self._current_context = None

    def _build_context(
        self,
        trade_date: date,
        carry: SessionCarry,
        provider_ctx: dict[str, Any],
    ) -> DayContext:
        """Synthesise DayContext from raw provider contributions."""
        regime_rv20 = _float_ctx(provider_ctx, "daily.regime_rv20")
        regime_dist_sma20 = _float_ctx(provider_ctx, "daily.regime_dist_sma20")
        regime_sma20_slope = _float_ctx(provider_ctx, "daily.regime_sma20_slope")
        regime_60d_return = _float_ctx(provider_ctx, "daily.regime_60d_return")
        vix_level = _float_ctx(provider_ctx, "daily.vix_level")

        # DayScore from daily features (primary signal)
        day_score, confidence, reason = self._synthesise_day_score(
            hint=str(provider_ctx.get("daily.day_score_hint", "")),
            llm_hint=str(provider_ctx.get("llm.day_assessment", "")),
            carry=carry,
            rv20=regime_rv20,
            slope=regime_sma20_slope,
        )

        # Size multiplier: reduced when carry losses are high
        size_mult = self._compute_size_multiplier(day_score, carry)

        return DayContext(
            trade_date=trade_date,
            day_score=day_score,
            day_score_confidence=confidence,
            day_score_reason=reason,
            regime_rv20=regime_rv20,
            regime_dist_sma20=regime_dist_sma20,
            regime_sma20_slope=regime_sma20_slope,
            regime_60d_return=regime_60d_return,
            vix_level=vix_level,
            provider_context=provider_ctx,
            session_carry=carry,
            size_multiplier=size_mult,
        )

    @staticmethod
    def _synthesise_day_score(
        *,
        hint: str,
        llm_hint: str,
        carry: SessionCarry,
        rv20: Optional[float],
        slope: Optional[float],
    ) -> tuple[DayScore, float, str]:
        """Combine all scoring inputs into a final DayScore."""
        reasons: list[str] = []

        # Hard avoid: 3+ consecutive losing days
        if carry.losing_streak_days >= 3:
            return DayScore.AVOID, 0.95, "losing_streak_days>=3"

        # LLM hint (highest priority when available and confident)
        llm_clean = llm_hint.strip().upper()
        if llm_clean in ("AVOID", "VOLATILE", "NEUTRAL", "CALM"):
            reasons.append(f"llm:{llm_clean}")
            try:
                llm_confidence = float(
                    # provider may have put confidence in context
                    0.75
                )
                return DayScore(llm_clean), llm_confidence, "+".join(reasons)
            except ValueError:
                pass

        # Daily feature hint from DailyFeaturesProvider
        hint_clean = hint.strip().upper()
        if hint_clean in ("AVOID", "VOLATILE", "NEUTRAL", "CALM"):
            reasons.append(f"daily_features:{hint_clean}")
            # Corroborate with raw rv20 / slope when available
            if rv20 is not None:
                reasons.append(f"rv20={rv20:.4f}")
            if slope is not None:
                reasons.append(f"slope={slope:.4f}")
            confidence = 0.80 if rv20 is not None else 0.60
            return DayScore(hint_clean), confidence, "+".join(reasons)

        # Carry-based degradation: 2 consecutive loss days → VOLATILE
        if carry.losing_streak_days >= 2:
            reasons.append("losing_streak_days>=2")
            return DayScore.VOLATILE, 0.70, "+".join(reasons)

        # No context available
        return DayScore.UNKNOWN, 0.0, "no_daily_context"

    @staticmethod
    def _compute_size_multiplier(day_score: DayScore, carry: SessionCarry) -> float:
        """Derive session-level size multiplier from day score and carry state."""
        base = {
            DayScore.CALM: 1.0,
            DayScore.NEUTRAL: 0.85,
            DayScore.VOLATILE: 0.50,
            DayScore.AVOID: 0.0,
            DayScore.UNKNOWN: 1.0,
        }.get(day_score, 1.0)

        # Reduce for carry losses: each prior consecutive losing session
        # reduces size by 15%, floored at 0.25 (never completely shut down
        # from carry alone unless day_score says AVOID).
        streak_penalty = carry.losing_streak_days * 0.15
        reduced = max(0.25, base - streak_penalty) if base > 0 else 0.0
        return round(reduced, 4)


def _float_ctx(ctx: dict[str, Any], key: str) -> Optional[float]:
    val = ctx.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


__all__ = ["BrainDecision", "TradingBrain"]
