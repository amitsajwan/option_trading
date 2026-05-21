"""Context providers for TradingBrain.

Each provider implements brain.plugin.ContextProvider and contributes
named key-value pairs to DayContext.provider_context at morning_briefing
time.

Built-in providers
------------------
DailyFeaturesProvider   — reads daily_regime_features.json (nightly build)
LLMContextProvider      — stub; no-op today; ready for future LLM wiring

Adding a new provider
---------------------
1.  Create a module here that implements ContextProvider.
2.  Instantiate it and pass to TradingBrain(context_providers=[...]).
The brain calls every provider at morning_briefing and merges the results.
"""

from .daily_features import DailyFeaturesProvider
from .llm_stub import LLMContextProvider

__all__ = ["DailyFeaturesProvider", "LLMContextProvider"]
