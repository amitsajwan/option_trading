"""ShadowAdapter — fires both a real Kite order (1 lot) and a paper fill simultaneously.

Selected by EXECUTION_ADAPTER=shadow.

Publishes fills to two separate Redis streams:
  execution:fills:real:v1  — real Kite fill (1 lot, capped by SHADOW_MAX_LOTS)
  execution:fills:paper:v1 — paper fill (full lot count from signal)

The FillTracker reads both streams. The dashboard can show side-by-side:
  Real P&L vs Paper P&L, and compute slippage = real_fill - paper_fill per trade.

Run for 5+ trading days before E1-S6 (production cutover).

Env vars:
  SHADOW_MAX_LOTS   int  (default 1)  — caps real Kite orders during shadow validation
"""

from __future__ import annotations

import logging
import os

from strategy_app.contracts import PositionContext, TradeSignal

from .base import BrokerAdapter, OrderResult
from .kite import KiteAdapter
from .paper import PaperAdapter

logger = logging.getLogger(__name__)

_SHADOW_MAX_LOTS = int(os.getenv("SHADOW_MAX_LOTS", "1") or "1")

# Redis stream names for real vs paper fills in shadow mode
SHADOW_REAL_FILLS_STREAM = os.getenv("SHADOW_REAL_FILLS_STREAM", "execution:fills:real:v1")
SHADOW_PAPER_FILLS_STREAM = os.getenv("SHADOW_PAPER_FILLS_STREAM", "execution:fills:paper:v1")


class _ShadowSignal:
    """Wraps a signal with lots capped at SHADOW_MAX_LOTS for the real order."""

    def __init__(self, signal: TradeSignal, max_lots: int) -> None:
        self._signal = signal
        self._max_lots = max_lots

    def __getattr__(self, name: str):
        return getattr(self._signal, name)

    @property
    def max_lots(self) -> int:
        return min(self._max_lots, self._signal.max_lots)


class ShadowAdapter(BrokerAdapter):
    """Dual-mode: real Kite (1 lot) + paper (full size) per signal."""

    def __init__(self) -> None:
        self._kite = KiteAdapter()
        self._paper = PaperAdapter()
        logger.info(
            "ShadowAdapter initialised: real_max_lots=%d streams real=%s paper=%s",
            _SHADOW_MAX_LOTS, SHADOW_REAL_FILLS_STREAM, SHADOW_PAPER_FILLS_STREAM,
        )

    def place_entry(self, signal: TradeSignal) -> OrderResult:
        # Real order capped at SHADOW_MAX_LOTS
        real_signal = _ShadowSignal(signal, _SHADOW_MAX_LOTS)
        real_result = self._kite.place_entry(real_signal)
        logger.info(
            "shadow entry — real: order_id=%s status=%s lots=%d",
            real_result.order_id, real_result.status, real_signal.max_lots,
        )

        # Paper fill at full size — always succeeds
        paper_result = self._paper.place_entry(signal)

        # Attach the paper result as a side attribute so the consumer can emit both
        real_result._shadow_paper_result = paper_result  # type: ignore[attr-defined]
        real_result._is_shadow = True                    # type: ignore[attr-defined]
        return real_result

    def place_exit(self, signal: TradeSignal, position: PositionContext) -> OrderResult:
        real_signal = _ShadowSignal(signal, _SHADOW_MAX_LOTS)
        real_result = self._kite.place_exit(real_signal, position)
        paper_result = self._paper.place_exit(signal, position)
        real_result._shadow_paper_result = paper_result  # type: ignore[attr-defined]
        real_result._is_shadow = True                    # type: ignore[attr-defined]
        return real_result

    def get_order_status(self, order_id: str) -> OrderResult:
        return self._kite.get_order_status(order_id)

    def cancel_order(self, order_id: str) -> bool:
        return self._kite.cancel_order(order_id)
