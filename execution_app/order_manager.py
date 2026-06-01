"""OrderManager — place an order, poll until filled or timeout.

The broker adapter's place_entry/place_exit returns status="placed".
OrderManager polls get_order_status() until the order is filled,
rejected, or the deadline is exceeded.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .adapter.base import BrokerAdapter, OrderResult

logger = logging.getLogger(__name__)

# Max time to wait for a fill before declaring timeout (seconds)
_DEFAULT_FILL_TIMEOUT_SEC = float(30)
# Interval between status polls (seconds)
_DEFAULT_POLL_INTERVAL_SEC = float(1)


@dataclass
class FillEvent:
    """Canonical fill record published to Redis stream execution:fills:v1."""

    order_id: str
    signal_id: str
    signal_type: str          # ENTRY | EXIT
    position_id: Optional[str]
    direction: Optional[str]
    strike: Optional[int]
    status: str               # filled | rejected | timeout | cancelled
    fill_price: Optional[float]
    fill_qty: Optional[int]
    signal_premium: Optional[float]   # premium at signal time
    slippage_pct: Optional[float]     # (fill_price - signal_premium) / signal_premium
    error: Optional[str]
    filled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "signal_id": self.signal_id,
            "signal_type": self.signal_type,
            "position_id": self.position_id,
            "direction": self.direction,
            "strike": self.strike,
            "status": self.status,
            "fill_price": self.fill_price,
            "fill_qty": self.fill_qty,
            "signal_premium": self.signal_premium,
            "slippage_pct": self.slippage_pct,
            "error": self.error,
            "filled_at": self.filled_at,
        }


class OrderManager:
    """Place order via adapter and block until fill confirmed or timeout."""

    def __init__(
        self,
        adapter: BrokerAdapter,
        fill_timeout_sec: float = _DEFAULT_FILL_TIMEOUT_SEC,
        poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
    ):
        self._adapter = adapter
        self._fill_timeout = fill_timeout_sec
        self._poll_interval = poll_interval_sec

    def place_and_confirm(
        self,
        *,
        order_result: OrderResult,
        signal_id: str,
        signal_type: str,
        position_id: Optional[str] = None,
        direction: Optional[str] = None,
        strike: Optional[int] = None,
        signal_premium: Optional[float] = None,
    ) -> FillEvent:
        """Given an already-placed order result, poll until filled or timeout."""
        if order_result.is_rejected:
            return FillEvent(
                order_id=order_result.order_id,
                signal_id=signal_id,
                signal_type=signal_type,
                position_id=position_id,
                direction=direction,
                strike=strike,
                status="rejected",
                fill_price=None,
                fill_qty=None,
                signal_premium=signal_premium,
                slippage_pct=None,
                error=order_result.error,
            )

        # Paper adapter returns filled immediately
        if order_result.is_filled:
            return self._make_fill_event(
                order_result=order_result,
                signal_id=signal_id,
                signal_type=signal_type,
                position_id=position_id,
                direction=direction,
                strike=strike,
                signal_premium=signal_premium,
            )

        # Real broker: poll until fill or timeout
        deadline = time.monotonic() + self._fill_timeout
        while time.monotonic() < deadline:
            time.sleep(self._poll_interval)
            status = self._adapter.get_order_status(order_result.order_id)
            if status.is_filled:
                return self._make_fill_event(
                    order_result=status,
                    signal_id=signal_id,
                    signal_type=signal_type,
                    position_id=position_id,
                    direction=direction,
                    strike=strike,
                    signal_premium=signal_premium,
                )
            if status.is_rejected:
                logger.warning("order rejected: order_id=%s error=%s", order_result.order_id, status.error)
                return FillEvent(
                    order_id=order_result.order_id,
                    signal_id=signal_id,
                    signal_type=signal_type,
                    position_id=position_id,
                    direction=direction,
                    strike=strike,
                    status="rejected",
                    fill_price=None,
                    fill_qty=None,
                    signal_premium=signal_premium,
                    slippage_pct=None,
                    error=status.error,
                )

        logger.warning("order fill timeout: order_id=%s signal_id=%s", order_result.order_id, signal_id)
        return FillEvent(
            order_id=order_result.order_id,
            signal_id=signal_id,
            signal_type=signal_type,
            position_id=position_id,
            direction=direction,
            strike=strike,
            status="timeout",
            fill_price=None,
            fill_qty=None,
            signal_premium=signal_premium,
            slippage_pct=None,
            error=f"fill not confirmed within {self._fill_timeout}s",
        )

    def _make_fill_event(
        self,
        *,
        order_result: OrderResult,
        signal_id: str,
        signal_type: str,
        position_id: Optional[str],
        direction: Optional[str],
        strike: Optional[int],
        signal_premium: Optional[float],
    ) -> FillEvent:
        slippage_pct: Optional[float] = None
        if signal_premium and order_result.fill_price and signal_premium > 0:
            slippage_pct = (order_result.fill_price - signal_premium) / signal_premium

        if slippage_pct is not None and abs(slippage_pct) > 0.001:
            logger.info(
                "fill slippage: order_id=%s signal_premium=%.2f fill=%.2f slip=%.3f%%",
                order_result.order_id,
                signal_premium or 0,
                order_result.fill_price or 0,
                slippage_pct * 100,
            )

        return FillEvent(
            order_id=order_result.order_id,
            signal_id=signal_id,
            signal_type=signal_type,
            position_id=position_id,
            direction=direction,
            strike=strike,
            status="filled",
            fill_price=order_result.fill_price,
            fill_qty=order_result.fill_qty,
            signal_premium=signal_premium,
            slippage_pct=slippage_pct,
            error=None,
        )
