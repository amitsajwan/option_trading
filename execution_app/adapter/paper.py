"""PaperAdapter — simulates fills at signal.entry_premium.

Behaviour is identical to the current paper trading simulation:
  - Entry fill at entry_premium (the snapshot price when signal fired)
  - Exit fill at whatever premium is passed in the exit signal
  - No real orders placed

Selected by EXECUTION_ADAPTER=paper (the default).
"""

from __future__ import annotations

import logging

from strategy_app.constants import BANKNIFTY_LOT_SIZE
from strategy_app.contracts import PositionContext, TradeSignal

from .base import BrokerAdapter, OrderResult

logger = logging.getLogger(__name__)


class PaperAdapter(BrokerAdapter):
    def place_entry(self, signal: TradeSignal) -> OrderResult:
        fill_price = signal.entry_premium or 0.0
        fill_qty = signal.max_lots * BANKNIFTY_LOT_SIZE
        logger.info(
            "paper entry: dir=%s strike=%s premium=%.2f lots=%d qty=%d signal_id=%s",
            signal.direction, signal.strike, fill_price, signal.max_lots, fill_qty, signal.signal_id,
        )
        return OrderResult(
            order_id=f"paper_{signal.signal_id}",
            status="filled",
            fill_price=fill_price,
            fill_qty=fill_qty,
            error=None,
        )

    def place_exit(self, signal: TradeSignal, position: PositionContext) -> OrderResult:
        fill_price = signal.entry_premium or position.current_premium or position.entry_premium
        fill_qty = position.lots * BANKNIFTY_LOT_SIZE
        logger.info(
            "paper exit: pos=%s pnl=%.3f premium=%.2f qty=%d signal_id=%s",
            position.position_id, position.pnl_pct, fill_price, fill_qty, signal.signal_id,
        )
        return OrderResult(
            order_id=f"paper_exit_{signal.signal_id}",
            status="filled",
            fill_price=fill_price,
            fill_qty=fill_qty,
            error=None,
        )

    def get_order_status(self, order_id: str) -> OrderResult:
        # Paper orders are always immediately filled; status polls are no-ops.
        return OrderResult(order_id=order_id, status="filled", fill_price=None, fill_qty=None, error=None)

    def cancel_order(self, order_id: str) -> bool:
        logger.warning("paper cancel_order called for %s (no-op)", order_id)
        return False
