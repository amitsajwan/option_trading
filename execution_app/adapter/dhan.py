"""DhanAdapter — places real NFO orders via Dhan (DhanHQ v2 REST API).

Selected by EXECUTION_ADAPTER=dhan.

Why raw REST (not the dhanhq SDK): the exact request format below was verified
live against the account, and stdlib urllib means zero new dependencies. SDK
signatures drift between versions; the v2 HTTP contract is stable.

Env vars required:
  DHAN_CLIENT_ID       — numeric client id (e.g. 1100XXXXXX)
  DHAN_ACCESS_TOKEN    — JWT from web.dhan.co -> Profile -> DhanHQ Trading APIs
                         (pick the longest validity; static IP must be whitelisted
                          for order placement)
Env vars optional:
  DHAN_PRODUCT_TYPE    — MARGIN (carry, default; mirrors Kite NRML) | INTRADAY
  DHAN_API_BASE        — override base url (default https://api.dhan.co/v2)
  DHAN_SCRIP_CACHE     — path for the instrument-master cache (default in /tmp)

Dhan identifies instruments by a numeric securityId, NOT a tradingsymbol. We
resolve (expiry, strike, CE/PE) -> securityId from Dhan's public scrip master,
which also carries the authoritative lot size (SEM_LOT_UNITS).

SAFETY: if Dhan's lot size disagrees with strategy_app's BANKNIFTY_LOT_SIZE the
adapter REJECTS the order rather than send a wrong-sized position with real
money. Reconcile the constant before going live.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import urllib.request
from datetime import date, datetime
from typing import Optional

from strategy_app.constants import resolve_lot_size
from strategy_app.contracts import PositionContext, TradeSignal

from .base import BrokerAdapter, OrderResult

logger = logging.getLogger(__name__)

_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
_EXCHANGE_SEGMENT = "NSE_FNO"

# Underlying this execution container trades — from STRATEGY_INSTRUMENT so a
# NIFTY container filters the scrip master for NIFTY contracts. Defaults to the
# primary (BANKNIFTY) when unset, preserving existing behavior.
try:
    from contracts_app import current_instrument as _current_instrument
    _UNDERLYING = _current_instrument()
except Exception:
    _UNDERLYING = "BANKNIFTY"


# ─────────────────────────────────────────────────────────────────────────────
# Scrip master: (expiry, strike, option_type) -> (security_id, lot_units)
# ─────────────────────────────────────────────────────────────────────────────
class _ScripMaster:
    """Lazily downloads Dhan's instrument master and indexes BANKNIFTY options.

    Refreshed once per UTC day. Thread-safe.
    """

    def __init__(self, url: str = _SCRIP_MASTER_URL, cache_path: str | None = None):
        self._url = url
        self._cache_path = cache_path or os.getenv(
            "DHAN_SCRIP_CACHE", os.path.join(os.getenv("TMPDIR", "/tmp"), "dhan_scrip_master.csv")
        )
        self._lock = threading.Lock()
        self._index: dict[tuple[str, int, str], tuple[str, int]] = {}
        self._loaded_for: Optional[date] = None

    def _raw_csv(self) -> str:
        """Return CSV text, using a same-day on-disk cache if present."""
        today = datetime.utcnow().date()
        try:
            if os.path.exists(self._cache_path):
                mtime = datetime.utcfromtimestamp(os.path.getmtime(self._cache_path)).date()
                if mtime == today:
                    with open(self._cache_path, "r", encoding="utf-8", errors="replace") as fh:
                        return fh.read()
        except OSError:
            pass
        logger.info("dhan scrip master: downloading %s", self._url)
        data = urllib.request.urlopen(self._url, timeout=60).read().decode("utf-8", errors="replace")
        try:
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                fh.write(data)
        except OSError as exc:
            logger.warning("dhan scrip master: cache write failed (%s) — continuing in-memory", exc)
        return data

    def _ensure_loaded(self) -> None:
        today = datetime.utcnow().date()
        if self._loaded_for == today and self._index:
            return
        with self._lock:
            if self._loaded_for == today and self._index:
                return
            index: dict[tuple[str, int, str], tuple[str, int]] = {}
            reader = csv.DictReader(io.StringIO(self._raw_csv()))
            for row in reader:
                if (row.get("SEM_INSTRUMENT_NAME") or "") != "OPTIDX":
                    continue
                if _UNDERLYING not in (row.get("SEM_TRADING_SYMBOL") or "").upper():
                    continue
                opt = (row.get("SEM_OPTION_TYPE") or "").upper()
                if opt not in ("CE", "PE"):
                    continue
                expiry = self._parse_expiry(row.get("SEM_EXPIRY_DATE"))
                strike = self._parse_strike(row.get("SEM_STRIKE_PRICE"))
                sec_id = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
                lot = self._parse_lot(row.get("SEM_LOT_UNITS"))
                if not expiry or strike is None or not sec_id or not lot:
                    continue
                index[(expiry, strike, opt)] = (sec_id, lot)
            self._index = index
            self._loaded_for = today
            logger.info("dhan scrip master: indexed %d BANKNIFTY option contracts", len(index))

    @staticmethod
    def _parse_expiry(v: Optional[str]) -> Optional[str]:
        # "2026-06-30 14:30:00" -> "2026-06-30"
        if not v:
            return None
        return str(v).split(" ")[0].strip() or None

    @staticmethod
    def _parse_strike(v: Optional[str]) -> Optional[int]:
        try:
            return int(round(float(v)))  # "65400.00000" -> 65400
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_lot(v: Optional[str]) -> Optional[int]:
        try:
            return int(round(float(v)))  # "30.0" -> 30
        except (TypeError, ValueError):
            return None

    def resolve(self, expiry: date, strike: int, option_type: str) -> Optional[tuple[str, int]]:
        """Return (security_id, lot_units) or None if not found."""
        self._ensure_loaded()
        return self._index.get((expiry.isoformat(), int(strike), option_type.upper()))


# ─────────────────────────────────────────────────────────────────────────────
# Adapter
# ─────────────────────────────────────────────────────────────────────────────
class DhanAdapter(BrokerAdapter):
    def __init__(self, client_id: str | None = None, access_token: str | None = None):
        self._client_id = client_id or os.getenv("DHAN_CLIENT_ID", "")
        self._token = access_token or os.getenv("DHAN_ACCESS_TOKEN", "")
        if not self._client_id or not self._token:
            raise ValueError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set for DhanAdapter")
        self._base = os.getenv("DHAN_API_BASE", "https://api.dhan.co/v2").rstrip("/")
        self._product = os.getenv("DHAN_PRODUCT_TYPE", "MARGIN").strip().upper()
        self._scrips = _ScripMaster()
        logger.info("DhanAdapter initialised (client_id=%s product=%s)", self._client_id, self._product)

    # ── HTTP helper ──────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body: Optional[dict] = None) -> tuple[int, dict]:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("access-token", self._token)
        req.add_header("client-id", self._client_id)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            payload = resp.read().decode() or "{}"
            return resp.status, json.loads(payload)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode() if exc.fp else ""
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"error": raw or str(exc)}

    # ── order quantity, with the safety guard ────────────────────────────────
    def _resolve_qty(self, expiry: date, strike: int, option_type: str, lots: int) -> tuple[str, int]:
        """Return (security_id, quantity). Raises ValueError on any unsafe condition."""
        resolved = self._scrips.resolve(expiry, strike, option_type)
        if resolved is None:
            raise ValueError(f"no Dhan securityId for {_UNDERLYING} {expiry} {strike} {option_type}")
        security_id, lot_units = resolved
        expected_lot = resolve_lot_size()
        if lot_units != expected_lot:
            # Fail loud rather than place a wrong-sized live position.
            raise ValueError(
                f"lot-size mismatch: Dhan lot_units={lot_units} but "
                f"resolved lot size={expected_lot} (instrument={_UNDERLYING}); "
                f"reconcile before live trading"
            )
        return security_id, lots * lot_units

    def _place(self, *, security_id: str, qty: int, side: str, tag: str) -> OrderResult:
        body = {
            "dhanClientId": self._client_id,
            "transactionType": side,            # BUY | SELL
            "exchangeSegment": _EXCHANGE_SEGMENT,
            "productType": self._product,       # MARGIN | INTRADAY
            "orderType": "MARKET",
            "validity": "DAY",
            "securityId": security_id,
            "quantity": qty,
            "price": 0,
            "correlationId": tag[:25],
        }
        status, payload = self._request("POST", "/orders", body)
        if status in (200, 201) and payload.get("orderId"):
            order_id = str(payload["orderId"])
            logger.info("dhan %s placed: order_id=%s sec=%s qty=%d status=%s",
                        side, order_id, security_id, qty, payload.get("orderStatus"))
            return OrderResult(order_id=order_id, status="placed",
                               fill_price=None, fill_qty=None, error=None)
        err = payload.get("errorMessage") or payload.get("error") or json.dumps(payload)
        logger.warning("dhan %s rejected: sec=%s qty=%d http=%s err=%s", side, security_id, qty, status, err)
        return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None, error=str(err))

    # ── BrokerAdapter contract ───────────────────────────────────────────────
    def place_entry(self, signal: TradeSignal) -> OrderResult:
        if not signal.expiry or not signal.strike or not signal.direction:
            return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None,
                               error="missing expiry/strike/direction on signal")
        try:
            security_id, qty = self._resolve_qty(signal.expiry, signal.strike, signal.direction, signal.max_lots)
        except ValueError as exc:
            logger.warning("dhan entry blocked: %s", exc)
            return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None, error=str(exc))
        return self._place(security_id=security_id, qty=qty, side="BUY", tag=signal.signal_id)

    def place_exit(self, signal: TradeSignal, position: PositionContext) -> OrderResult:
        if not signal.expiry or not signal.strike or not signal.direction:
            return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None,
                               error="missing expiry/strike/direction on exit signal")
        try:
            security_id, qty = self._resolve_qty(signal.expiry, signal.strike, signal.direction, position.lots)
        except ValueError as exc:
            logger.warning("dhan exit blocked: pos=%s %s", position.position_id, exc)
            return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None, error=str(exc))
        return self._place(security_id=security_id, qty=qty, side="SELL", tag=signal.signal_id)

    def get_order_status(self, order_id: str) -> OrderResult:
        status, payload = self._request("GET", f"/orders/{order_id}")
        if status != 200:
            return OrderResult(order_id=order_id, status="unknown", fill_price=None, fill_qty=None,
                               error=payload.get("error") or f"http {status}")
        # /orders/{id} returns a list (order + its history) or a single object depending on state.
        order = payload[0] if isinstance(payload, list) and payload else payload
        dhan_status = str(order.get("orderStatus", "")).upper()
        if dhan_status == "TRADED":
            return OrderResult(
                order_id=order_id, status="filled",
                fill_price=float(order.get("averageTradedPrice") or order.get("price") or 0) or None,
                fill_qty=int(order.get("filledQty") or order.get("quantity") or 0) or None,
                error=None,
            )
        if dhan_status in ("REJECTED", "CANCELLED", "EXPIRED"):
            mapped = "cancelled" if dhan_status == "EXPIRED" else dhan_status.lower()
            return OrderResult(order_id=order_id, status=mapped, fill_price=None, fill_qty=None,
                               error=order.get("omsErrorDescription") or order.get("errorMessage"))
        # PENDING / TRANSIT / PART_TRADED → not terminal yet
        return OrderResult(order_id=order_id, status=dhan_status.lower() or "unknown",
                           fill_price=None, fill_qty=None, error=None)

    def cancel_order(self, order_id: str) -> bool:
        status, payload = self._request("DELETE", f"/orders/{order_id}")
        if status == 200:
            logger.info("dhan cancel_order: order_id=%s status=%s", order_id, payload.get("orderStatus"))
            return True
        logger.warning("dhan cancel_order failed: order_id=%s http=%s payload=%s", order_id, status, payload)
        return False
