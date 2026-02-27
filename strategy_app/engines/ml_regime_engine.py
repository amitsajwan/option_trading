"""ML regime engine contract implementation placeholder."""

from __future__ import annotations

from datetime import date
from typing import Optional

from ..contracts import SnapshotPayload, StrategyEngine, TradeSignal


class MLRegimeEngine(StrategyEngine):
    """Phase-3 placeholder. Replace internals with model inference logic."""

    def __init__(self, delegate: Optional[StrategyEngine] = None) -> None:
        self._delegate = delegate

    def on_session_start(self, trade_date: date) -> None:
        if self._delegate is not None:
            self._delegate.on_session_start(trade_date)

    def on_session_end(self, trade_date: date) -> None:
        if self._delegate is not None:
            self._delegate.on_session_end(trade_date)

    def evaluate(self, snapshot: SnapshotPayload) -> Optional[TradeSignal]:
        if self._delegate is not None:
            return self._delegate.evaluate(snapshot)
        return None
