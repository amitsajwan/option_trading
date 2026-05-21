"""Extension contracts for the TradingBrain.

Every new capability — daily features, LLM context, news sentiment, a new
strategy — is wired in by implementing one of these ABCs and registering it
with TradingBrain.  Nothing else in the engine needs to change.

Extension points
----------------
ContextProvider
    Runs once per day at morning_briefing time.  Returns a flat dict of
    key-value pairs that are merged into DayContext.provider_context.
    Examples: DailyFeaturesProvider, LLMContextProvider, NewsProvider.

StrategyPlugin
    Knows how well it fits a given DayContext.  Used by
    StrategyFitnessEvaluator to prune the active strategy set before the
    session starts.  Any BaseStrategy already in the StrategyRouter can
    optionally implement this for context-aware fitness scoring.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import DayContext, FitnessScore


class ContextProvider(ABC):
    """Contributes named key-value context for a trading day.

    Called once at morning_briefing time before session open.
    Must be fast (< 1 s) and must never raise — return {} on failure.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier used in logs and debug output."""

    @abstractmethod
    def provide(self, trade_date: date) -> dict[str, Any]:
        """Return context entries for *trade_date*.

        Keys should be prefixed with the provider name to avoid collisions,
        e.g. ``{"daily.regime_rv20": 0.012, "daily.sma20_slope": 0.003}``.
        An empty dict is a valid no-op return.
        """


class StrategyPlugin(ABC):
    """Context-aware fitness interface for a strategy.

    Strategies that implement this can be dynamically included or excluded
    from the active set based on DayContext (macro regime, carry state, etc.).
    Strategies that do *not* implement this are always included.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Must match the strategy name in StrategyRouter._strategy_registry."""

    @abstractmethod
    def fits(self, context: "DayContext") -> "FitnessScore":
        """Score how well this strategy fits today's context.

        Returns a FitnessScore with ``fits=True/False`` and a ``score``
        in [0, 1].  ``size_multiplier`` adjusts lot sizing (1.0 = normal,
        0.5 = half size, 0.0 = skip entirely).
        """


__all__ = ["ContextProvider", "StrategyPlugin"]
