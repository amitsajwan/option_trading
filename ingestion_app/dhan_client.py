"""
Dhan live data client for the ingestion layer.

Covers: intraday OHLC, real-time market quotes, option chain.
Auth: access-token header (JWT, 24 h, refreshable via /v2/RenewToken).

Key Dhan security IDs (IDX_I segment):
  BankNifty index: 25
  India VIX:       21
  Nifty index:     13
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger("dhan_live_client")

IST = timezone(timedelta(hours=5, minutes=30))
DHAN_BASE = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# Well-known security IDs for IDX_I segment (stable, confirmed from scrip master)
IDX_BANKNIFTY = "25"
IDX_NIFTY     = "13"
IDX_VIX       = "21"


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _to_float(v: Any) -> float:
    try:
        return float(v) if v is not None else float("nan")
    except Exception:
        return float("nan")


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(round(f)) if math.isfinite(f) else None


class DhanLiveClient:
    """Rate-limited Dhan REST client for live ingestion (4 rps default)."""

    def __init__(self, token: str, client_id: str, rps: float = 4.0):
        if not token or not client_id:
            raise ValueError("DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID are required")
        self._token = token
        self._client_id = client_id
        self._interval = 1.0 / max(0.1, rps)
        self._last_call = 0.0
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json",
        })

    def _throttle(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last_call
            if gap < self._interval:
                time.sleep(self._interval - gap)
            self._last_call = time.monotonic()

    def _post(self, path: str, payload: dict, retries: int = 3) -> Any:
        url = f"{DHAN_BASE}{path}"
        for attempt in range(retries):
            self._throttle()
            try:
                r = self._session.post(url, json=payload, timeout=30)
                if r.status_code == 429:
                    wait = 2 ** attempt * 3
                    log.warning("rate-limited, sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                log.warning("timeout %s attempt %d/%d", path, attempt + 1, retries)
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Dhan API {path} failed after {retries} attempts")

    def _get(self, path: str, params: Optional[dict] = None, retries: int = 3) -> Any:
        url = f"{DHAN_BASE}{path}"
        for attempt in range(retries):
            self._throttle()
            try:
                r = self._session.get(url, params=params, timeout=30)
                if r.status_code == 429:
                    time.sleep(2 ** attempt * 3)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Dhan API GET {path} failed after {retries} attempts")

    def validate_token(self) -> bool:
        try:
            r = self._session.get(f"{DHAN_BASE}/profile", timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def renew_token(self) -> Optional[str]:
        """Extend token TTL by 24 h without full TOTP re-auth."""
        try:
            resp = self._get("/RenewToken")
            new_token = (resp or {}).get("accessToken") or (resp or {}).get("access_token")
            if new_token:
                self._token = new_token
                self._session.headers.update({"access-token": new_token})
                log.info("Dhan token renewed successfully")
            return new_token
        except Exception as e:
            log.warning("Token renewal failed: %s", e)
            return None

    # ── OHLC history ─────────────────────────────────────────────────────────

    def get_intraday_ohlc(
        self,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        from_dt: datetime,
        to_dt: datetime,
        interval: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Fetch 1-min (or other) intraday bars via /v2/charts/intraday.
        Max range = 90 days per request. Returns list of OHLCV dicts with 'start_at'.
        """
        from_str = from_dt.strftime("%Y-%m-%d %H:%M:%S")
        to_str   = to_dt.strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "securityId":      security_id,
            "exchangeSegment": exchange_segment,
            "instrument":      instrument,
            "interval":        str(interval),
            "fromDate":        from_str,
            "toDate":          to_str,
        }
        resp = self._post("/charts/intraday", payload)
        return _parse_intraday_response(resp, exchange_segment)

    def get_option_chain(
        self,
        underlying: str,
        expiry_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Fetch option chain from /v2/optionchain.
        underlying: "BANKNIFTY" or "NIFTY"
        expiry_date: "YYYY-MM-DD" (nearest weekly if omitted)
        """
        payload: Dict[str, Any] = {
            "UnderlyingScrip": underlying.upper(),
            "UnderlyingSeg":   "NSE_FNO",
        }
        if expiry_date:
            payload["ExpiryDate"] = expiry_date
        return self._post("/optionchain", payload) or {}

    def get_quotes(self, securities: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        Fetch real-time quotes from /v2/marketfeed/quote.
        securities: list of {"exchangeSegment": ..., "securityId": ..., "instrument": ...}
        Returns list of quote dicts.
        """
        payload = {"securities": securities}
        resp = self._post("/marketfeed/quote", payload)
        if isinstance(resp, dict):
            return resp.get("data", []) or []
        return []


# ── Response parsers ──────────────────────────────────────────────────────────

def _parse_intraday_response(resp: Any, segment: str) -> List[Dict[str, Any]]:
    """Parse Dhan /v2/charts/intraday response into normalized bar dicts."""
    if not isinstance(resp, dict):
        return []

    timestamps = resp.get("timestamp") or []
    opens      = resp.get("open")   or []
    highs      = resp.get("high")   or []
    lows       = resp.get("low")    or []
    closes     = resp.get("close")  or []
    volumes    = resp.get("volume") or []
    ois        = resp.get("oi")     or []

    n = len(timestamps)
    if n == 0:
        return []

    bars = []
    for i in range(n):
        ts_raw = timestamps[i]
        try:
            # Dhan timestamps are Unix epoch seconds
            dt = datetime.fromtimestamp(int(ts_raw), tz=IST)
        except Exception:
            continue

        bar: Dict[str, Any] = {
            "start_at": dt.isoformat(),
            "open":   _to_float(opens[i]   if i < len(opens)   else None),
            "high":   _to_float(highs[i]   if i < len(highs)   else None),
            "low":    _to_float(lows[i]    if i < len(lows)    else None),
            "close":  _to_float(closes[i]  if i < len(closes)  else None),
            "volume": _to_int(volumes[i]   if i < len(volumes) else None) or 0,
            "oi":     _to_int(ois[i]       if i < len(ois)     else None),
        }
        bars.append(bar)

    return bars


# ── Scrip master ──────────────────────────────────────────────────────────────

_SCRIP_CACHE: Optional["ScripMaster"] = None
_SCRIP_LOCK = threading.Lock()


class ScripMaster:
    """
    Cached Dhan scrip master. Downloads once per process (or once per day from disk).
    Use get() to look up securityId by tradingsymbol.
    """

    def __init__(self, rows: List[Dict[str, str]]):
        # Index by SEM_TRADING_SYMBOL (upper)
        self._by_symbol: Dict[str, Dict[str, str]] = {}
        self._futures: List[Dict[str, str]] = []
        for row in rows:
            sym = str(row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
            if sym:
                self._by_symbol[sym] = row
            inst = str(row.get("SEM_INSTRUMENT_NAME") or "").strip().upper()
            if inst == "FUTIDX":
                self._futures.append(row)

    @classmethod
    def load(cls, cache_dir: str = "/tmp") -> "ScripMaster":
        global _SCRIP_CACHE
        with _SCRIP_LOCK:
            if _SCRIP_CACHE is not None:
                return _SCRIP_CACHE
            cache_path = Path(cache_dir) / "dhan_scrip_master.csv"
            # Refresh if older than 12 hours
            if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < 43200:
                log.info("Loading scrip master from cache %s", cache_path)
                import csv
                rows = list(csv.DictReader(cache_path.open(encoding="utf-8")))
            else:
                log.info("Downloading Dhan scrip master...")
                try:
                    r = requests.get(SCRIP_MASTER_URL, timeout=60)
                    r.raise_for_status()
                    cache_path.write_bytes(r.content)
                    import csv
                    rows = list(csv.DictReader(io.StringIO(r.text, newline="")))
                    log.info("Scrip master downloaded: %d rows", len(rows))
                except Exception as e:
                    log.error("Scrip master download failed: %s", e)
                    rows = []
            _SCRIP_CACHE = cls(rows)
            return _SCRIP_CACHE

    def lookup(self, trading_symbol: str) -> Optional[Dict[str, str]]:
        return self._by_symbol.get(str(trading_symbol or "").strip().upper())

    def find_nearest_futures(self, underlying: str) -> Optional[Dict[str, str]]:
        """Find the nearest-expiry FUTIDX row for the given underlying (e.g. 'BANKNIFTY')."""
        underlying_u = str(underlying or "").strip().upper()
        today = date.today()
        candidates = []
        for row in self._futures:
            name = str(row.get("SEM_CUSTOM_SYMBOL") or row.get("SEM_TRADING_SYMBOL") or "").upper()
            if not name.startswith(underlying_u):
                continue
            exp_str = str(row.get("SEM_EXPIRY_DATE") or "").strip()
            try:
                exp_dt = datetime.fromisoformat(exp_str[:10]).date()
            except Exception:
                continue
            if exp_dt >= today:
                candidates.append((exp_dt, row))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
