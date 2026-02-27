"""Deterministic rule engine placeholder for Layer 4."""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from ..contracts import SnapshotPayload, StrategyEngine, TradeSignal

logger = logging.getLogger(__name__)


class DeterministicRuleEngine(StrategyEngine):
    """
    Phase-1 deterministic engine.

    Current behavior is no-trade by default; this preserves contract wiring
    while trigger logic is added incrementally.
    """

    def __init__(self) -> None:
        self._current_session: Optional[date] = None

    def on_session_start(self, trade_date: date) -> None:
        self._current_session = trade_date
        logger.info("deterministic engine session started: %s", trade_date.isoformat())

    def on_session_end(self, trade_date: date) -> None:
        logger.info("deterministic engine session ended: %s", trade_date.isoformat())
        if self._current_session == trade_date:
            self._current_session = None

    def evaluate(self, snapshot: SnapshotPayload) -> Optional[TradeSignal]:
        _ = snapshot
        return None
