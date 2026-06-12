"""LegGateway — the per-leg order interface the SafeExecutor drives.

Two implementations:
  PaperLegGateway — simulates fills from a price source (for paper-trading + tests).
  DhanLegGateway  — wraps the existing DhanAdapter (real money; used only after paper).

A 'leg' is (action BUY|SELL, option_type CE|PE, strike, expiry, qty). execute() is
SYNCHRONOUS: it submits and waits for the fill, returning Fill(filled, price). This is
what lets the executor do strict buy-first / confirm-each-fill / unwind-on-fail.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class Fill:
    filled: bool
    price: float
    order_id: str = ""
    error: str = ""


class LegGateway(ABC):
    @abstractmethod
    def execute(self, action: str, option_type: str, strike: int, expiry: date, qty: int) -> Fill:
        """Submit a single leg and block until filled/failed. Never raises."""


class PaperLegGateway(LegGateway):
    """Paper fills from a price source. price_fn(option_type, strike) -> ltp (or None)."""

    def __init__(self, price_fn: Callable[[str, int], Optional[float]], slippage_pts: float = 1.0):
        self._price = price_fn
        self._slip = float(slippage_pts)

    def execute(self, action: str, option_type: str, strike: int, expiry: date, qty: int) -> Fill:
        ltp = self._price(option_type, strike)
        if ltp is None or ltp <= 0:
            return Fill(False, 0.0, error=f"paper: no price {option_type}{strike}")
        # buyer pays up half-spread, seller receives less — conservative
        fill = ltp + self._slip if action == "BUY" else max(0.05, ltp - self._slip)
        return Fill(True, round(fill, 2), order_id=f"paper-{action}-{option_type}{strike}")


class DhanLegGateway(LegGateway):
    """Real Dhan leg execution. Reuses the existing DhanAdapter's scrip master + order
    placement + status polling. Used ONLY after paper-validation (real money)."""

    def __init__(self, dhan_adapter, poll_timeout_s: float = 20.0, poll_interval_s: float = 1.0):
        self._a = dhan_adapter
        self._timeout = poll_timeout_s
        self._interval = poll_interval_s

    def execute(self, action: str, option_type: str, strike: int, expiry: date, qty: int) -> Fill:
        try:
            security_id, _ = self._a._resolve_qty(expiry, strike, option_type, 0)  # validates + securityId
        except Exception as exc:  # lot-size/symbol guard
            return Fill(False, 0.0, error=f"dhan resolve: {exc}")
        res = self._a._place(security_id=security_id, qty=qty, side=action, tag=f"{action}{option_type}{strike}")
        if res.status != "placed" or not res.order_id:
            return Fill(False, 0.0, error=res.error or "placement rejected")
        # poll to confirm the fill
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            st = self._a.get_order_status(res.order_id)
            if st.status == "filled":
                return Fill(True, float(st.fill_price or 0.0), order_id=res.order_id)
            if st.status in ("rejected", "cancelled"):
                return Fill(False, 0.0, order_id=res.order_id, error=st.error or st.status)
            time.sleep(self._interval)
        # timeout: cancel and report unfilled (executor will unwind)
        self._a.cancel_order(res.order_id)
        return Fill(False, 0.0, order_id=res.order_id, error="fill timeout")
