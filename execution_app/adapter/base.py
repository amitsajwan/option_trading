"""BrokerAdapter contract — the single interface between strategy signals and a broker."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from strategy_app.contracts import PositionContext, TradeSignal


@dataclass
class OrderResult:
    order_id: str
    status: str          # placed | filled | rejected | cancelled | unknown
    fill_price: Optional[float]
    fill_qty: Optional[int]
    error: Optional[str]

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"

    @property
    def is_rejected(self) -> bool:
        return self.status == "rejected"


class BrokerAdapter(ABC):
    """Broker abstraction.

    Switch paper ↔ live via EXECUTION_ADAPTER env var without changing strategy code.
    All methods are synchronous; long-running order polls happen in order_manager.
    """

    @abstractmethod
    def place_entry(self, signal: TradeSignal) -> OrderResult: ...

    @abstractmethod
    def place_exit(self, signal: TradeSignal, position: PositionContext) -> OrderResult: ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...
