"""KiteAdapter — places real NFO orders via Zerodha Kite Connect.

Selected by EXECUTION_ADAPTER=kite.

Env vars required:
  KITE_API_KEY        — from Kite developer console
  KITE_ACCESS_TOKEN   — refreshed daily (see E5-S2 auto-refresh)

Symbol convention for BANKNIFTY weekly options:
  BANKNIFTY{DDMMMYY}{STRIKE}{CE|PE}
  e.g. BANKNIFTY26JUN5400PE
"""

from __future__ import annotations

import logging
import os
from datetime import date

from kiteconnect import KiteConnect

from strategy_app.constants import BANKNIFTY_LOT_SIZE
from strategy_app.contracts import PositionContext, TradeSignal

from .base import BrokerAdapter, OrderResult

logger = logging.getLogger(__name__)

_MONTH_MAP = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def _build_nfo_symbol(expiry: date, strike: int, option_type: str) -> str:
    """Build Kite NFO tradingsymbol for BANKNIFTY weekly option.

    Format: BANKNIFTY{D}{MON}{YY}{STRIKE}{CE|PE}
    Example: expiry=2026-06-26, strike=54200, CE → BANKNIFTY26JUN2654200CE
    """
    day = expiry.day
    month = _MONTH_MAP[expiry.month]
    year = str(expiry.year)[2:]
    opt = option_type.upper()
    return f"BANKNIFTY{day}{month}{year}{strike}{opt}"


class KiteAdapter(BrokerAdapter):
    def __init__(self, api_key: str | None = None, access_token: str | None = None):
        _api_key = api_key or os.getenv("KITE_API_KEY", "")
        _token = access_token or os.getenv("KITE_ACCESS_TOKEN", "")
        if not _api_key or not _token:
            raise ValueError("KITE_API_KEY and KITE_ACCESS_TOKEN must be set for KiteAdapter")
        self._kite = KiteConnect(api_key=_api_key)
        self._kite.set_access_token(_token)
        logger.info("KiteAdapter initialised (api_key=%s...)", _api_key[:6])

    def place_entry(self, signal: TradeSignal) -> OrderResult:
        if not signal.expiry or not signal.strike or not signal.direction:
            return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None,
                               error="missing expiry/strike/direction on signal")
        tradingsymbol = _build_nfo_symbol(signal.expiry, signal.strike, signal.direction)
        qty = signal.max_lots * BANKNIFTY_LOT_SIZE
        try:
            order_id = self._kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=KiteConnect.EXCHANGE_NFO,
                tradingsymbol=tradingsymbol,
                transaction_type=KiteConnect.TRANSACTION_TYPE_BUY,
                quantity=qty,
                product=KiteConnect.PRODUCT_NRML,
                order_type=KiteConnect.ORDER_TYPE_MARKET,
                tag=signal.signal_id[:10],
            )
            logger.info("kite entry placed: order_id=%s symbol=%s qty=%d signal=%s",
                        order_id, tradingsymbol, qty, signal.signal_id)
            return OrderResult(order_id=str(order_id), status="placed",
                               fill_price=None, fill_qty=None, error=None)
        except Exception as exc:
            logger.warning("kite entry rejected: symbol=%s error=%s", tradingsymbol, exc)
            return OrderResult(order_id="", status="rejected",
                               fill_price=None, fill_qty=None, error=str(exc))

    def place_exit(self, signal: TradeSignal, position: PositionContext) -> OrderResult:
        if not signal.expiry or not signal.strike or not signal.direction:
            return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None,
                               error="missing expiry/strike/direction on exit signal")
        tradingsymbol = _build_nfo_symbol(signal.expiry, signal.strike, signal.direction)
        qty = position.lots * BANKNIFTY_LOT_SIZE
        try:
            order_id = self._kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=KiteConnect.EXCHANGE_NFO,
                tradingsymbol=tradingsymbol,
                transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=qty,
                product=KiteConnect.PRODUCT_NRML,
                order_type=KiteConnect.ORDER_TYPE_MARKET,
                tag=signal.signal_id[:10],
            )
            logger.info("kite exit placed: order_id=%s symbol=%s qty=%d pos=%s",
                        order_id, tradingsymbol, qty, position.position_id)
            return OrderResult(order_id=str(order_id), status="placed",
                               fill_price=None, fill_qty=None, error=None)
        except Exception as exc:
            logger.warning("kite exit rejected: symbol=%s pos=%s error=%s",
                           tradingsymbol, position.position_id, exc)
            return OrderResult(order_id="", status="rejected",
                               fill_price=None, fill_qty=None, error=str(exc))

    def get_order_status(self, order_id: str) -> OrderResult:
        try:
            orders = self._kite.orders()
            order = next((o for o in orders if str(o["order_id"]) == order_id), None)
            if order is None:
                return OrderResult(order_id=order_id, status="unknown",
                                   fill_price=None, fill_qty=None, error="not found")
            kite_status = order.get("status", "").upper()
            if kite_status == "COMPLETE":
                return OrderResult(
                    order_id=order_id,
                    status="filled",
                    fill_price=float(order.get("average_price", 0) or 0),
                    fill_qty=int(order.get("filled_quantity", 0) or 0),
                    error=None,
                )
            if kite_status in ("REJECTED", "CANCELLED"):
                return OrderResult(order_id=order_id, status=kite_status.lower(),
                                   fill_price=None, fill_qty=None,
                                   error=order.get("status_message"))
            return OrderResult(order_id=order_id, status=kite_status.lower(),
                               fill_price=None, fill_qty=None, error=None)
        except Exception as exc:
            logger.warning("kite get_order_status failed: order_id=%s error=%s", order_id, exc)
            return OrderResult(order_id=order_id, status="unknown",
                               fill_price=None, fill_qty=None, error=str(exc))

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._kite.cancel_order(variety=KiteConnect.VARIETY_REGULAR, order_id=order_id)
            logger.info("kite cancel_order: order_id=%s", order_id)
            return True
        except Exception as exc:
            logger.warning("kite cancel_order failed: order_id=%s error=%s", order_id, exc)
            return False
