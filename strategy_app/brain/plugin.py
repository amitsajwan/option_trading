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
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from .context import DayContext, FitnessScore


class RegimeDecisionResult(NamedTuple):
    """Typed output of a :class:`RegimePlugin` classification call.

    Importable independently of ``RegimePlugin`` to avoid circular imports
    when ``contracts_app.decision_events`` needs the type.
    """

    regime: str
    confidence: float
    evidence: dict[str, Any]
    plugin_id: str
    plugin_version: str


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


class DepthDecisionResult(NamedTuple):
    """Output of a :class:`DepthPlugin` evaluation.

    Depth acts as a **confidence modifier**, not just a pass/fail gate:
    - CE trade + strong CE bid → confidence_delta > 0 (depth aligned)
    - CE trade + heavy ask pressure → confidence_delta < 0 (depth disagrees)
    - Depth absent (replay / feed offline) → proceed=True, confidence_delta=None

    ``DEPTH_HARD_GATE=1`` enables hard rejection when depth strongly disagrees.
    """

    proceed: bool                        # True = continue to strike selection
    skip_reason: Optional[str]           # set when proceed=False
    confidence_delta: Optional[float]    # adjustment to upstream confidence; None = no change
    ce_bid_strength: Optional[float]     # CE bid qty / (bid+ask) qty, 0–1
    pe_bid_strength: Optional[float]     # PE bid qty / (bid+ask) qty, 0–1
    spread_pct: Optional[float]          # (ask - bid) / bid for the target side
    depth_aligned: bool                  # True when depth direction matches trade direction
    depth_available: bool                # False when feed is offline / stale
    plugin_id: str
    plugin_version: str


class DepthPlugin(ABC):
    """Evaluates option order-book depth before strike selection.

    Live deployments inject a concrete implementation that reads from the
    ``ingestion_app`` depth feed via :class:`~strategy_app.runtime.redis_depth_reader.RedisDepthReader`.
    Replay and paper-trading use the default :class:`~strategy_app.market.depth_plugin.PassthroughDepthPlugin`
    which always proceeds without blocking.

    Rules:
    - ``proceed=True`` whenever depth is absent (feed offline / replay) — never block.
    - ``proceed=False`` only when depth IS available AND quality is below threshold
      AND the consumer is configured with ``DEPTH_HARD_GATE=1``.
    """

    @property
    @abstractmethod
    def plugin_id(self) -> str: ...

    @property
    @abstractmethod
    def plugin_version(self) -> str: ...

    @abstractmethod
    def evaluate(
        self,
        direction: str,
        snapshot: dict[str, Any],
        context: dict[str, Any],
    ) -> DepthDecisionResult:
        """Evaluate depth quality for the given direction and snapshot.

        Args:
            direction: ``"CE"`` or ``"PE"``.
            snapshot:  Raw snapshot payload (same shape as MarketSnapshot).
            context:   Day context from :class:`ContextProvider` providers.

        Returns:
            :class:`DepthDecisionResult` — always with ``proceed=True`` when
            depth data is absent.
        """


class RegimePlugin(ABC):
    """Pluggable regime classifier for the stream-native pipeline.

    Implement this to supply custom or ML-backed regime classification.
    The default implementation is :class:`~strategy_app.market.regime_plugin_adapter.RegimeClassifierAdapter`
    which wraps the existing rule-based :class:`~strategy_app.market.regime.RegimeClassifier`.

    Registration::

        consumer = RegimeDecisionConsumer(bus=stage_bus, plugin=MyRegimePlugin())
    """

    @property
    @abstractmethod
    def plugin_id(self) -> str:
        """Stable identifier for this plugin, e.g. ``'regime_classifier_v1'``."""

    @property
    @abstractmethod
    def plugin_version(self) -> str:
        """Semver string, e.g. ``'1.0'``."""

    @abstractmethod
    def classify(
        self,
        snapshot: dict[str, Any],
        context: dict[str, Any],
    ) -> RegimeDecisionResult:
        """Classify the market regime for one snapshot.

        Args:
            snapshot: Raw snapshot payload dict (same shape as MarketSnapshot).
            context:  Per-day context from :class:`ContextProvider` providers.

        Returns:
            A :class:`RegimeDecisionResult` with ``regime``, ``confidence``,
            ``evidence``, ``plugin_id``, and ``plugin_version``.
        """


__all__ = [
    "ContextProvider",
    "StrategyPlugin",
    "DepthPlugin",
    "DepthDecisionResult",
    "RegimePlugin",
    "RegimeDecisionResult",
]
