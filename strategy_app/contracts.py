"""Layer 3 -> Layer 4 strategy contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

SnapshotPayload = dict[str, Any]


@dataclass
class TradeSignal:
    signal_id: str
    timestamp: datetime
    snapshot_id: str
    direction: str  # CE | PE
    strike: int
    expiry: date
    entry_premium: float
    stop_loss_pct: float
    target_pct: float
    max_lots: int
    source: str  # RULE | ML | HYBRID
    confidence: Optional[float]
    reason: str


class StrategyEngine(ABC):
    """Engine contract invoked by event bus consumer on every snapshot."""

    @abstractmethod
    def evaluate(self, snapshot: SnapshotPayload) -> Optional[TradeSignal]:
        """Evaluate one snapshot and optionally return a trade signal."""

    @abstractmethod
    def on_session_start(self, trade_date: date) -> None:
        """Session start hook."""

    @abstractmethod
    def on_session_end(self, trade_date: date) -> None:
        """Session end hook."""
