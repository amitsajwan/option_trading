"""Stream-native decision pipeline consumers (Phase 2).

Each consumer reads from one Redis Stream, executes one decision stage,
and writes to the next stream.  They are independent processes that
communicate only through events — no shared in-process state.

Consumer chain:
    market_snapshots
        → RegimeDecisionConsumer    → regime_decisions
        → EntryDecisionConsumer     → entry_decisions
        → DirectionDecisionConsumer → direction_decisions
        → DepthDecisionConsumer     → depth_decisions
        → StrikeDecisionConsumer    → strike_decisions
        → RiskDecisionConsumer      → risk_decisions
        → ExecutionConsumer         → execution_events
"""
from .regime_decision_consumer import RegimeDecisionConsumer
from .entry_decision_consumer import EntryDecisionConsumer
from .direction_decision_consumer import DirectionDecisionConsumer
from .depth_decision_consumer import DepthDecisionConsumer
from .strike_decision_consumer import StrikeDecisionConsumer
from .risk_decision_consumer import RiskDecisionConsumer
from .execution_consumer import ExecutionConsumer

__all__ = [
    "RegimeDecisionConsumer",
    "EntryDecisionConsumer",
    "DirectionDecisionConsumer",
    "DepthDecisionConsumer",
    "StrikeDecisionConsumer",
    "RiskDecisionConsumer",
    "ExecutionConsumer",
]
