"""TradingBrain — intelligent coordinator for the strategy engine.

Quick start
-----------
The brain is wired automatically into DeterministicRuleEngine when
``BRAIN_ENABLED=true`` (the default).  No other configuration is required.

To disable for debugging::

    BRAIN_ENABLED=false

To require 2-strategy consensus before entry::

    BRAIN_CONSENSUS_MIN_AGREEING=2

To use daily regime features (run the nightly builder first)::

    BRAIN_DAILY_FEATURES_PATH=/opt/option_trading/.run/daily_regime_features.json

Extension points
----------------
* Add a ContextProvider to contribute new morning context (LLM, news, etc.)
* Add a StrategyPlugin to express context-aware fitness for a strategy
* Subclass ConsensusGate to customise the agreement logic

See brain/plugin.py for the extension ABCs.
"""

from .brain import BrainDecision, TradingBrain
from .consensus import ConsensusGate, ConsensusResult
from .context import DayContext, DayScore, FitnessScore, SessionCarry
from .fitness import StrategyFitnessEvaluator
from .plugin import ContextProvider, StrategyPlugin
from .session_memory import SessionMemory

__all__ = [
    "BrainDecision",
    "ConsensusGate",
    "ConsensusResult",
    "ContextProvider",
    "DayContext",
    "DayScore",
    "FitnessScore",
    "SessionCarry",
    "SessionMemory",
    "StrategyFitnessEvaluator",
    "StrategyPlugin",
    "TradingBrain",
]
