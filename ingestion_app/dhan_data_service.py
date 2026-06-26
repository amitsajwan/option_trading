"""
DhanDataService — drop-in replacement for KiteDataService in api_service.py.

Provides the same get_tick / get_ohlc / get_option_chain interface that the
snapshot_app FastAPI endpoints consume.

Environment variables (set in .env.compose):
  DHAN_ACCESS_TOKEN   — JWT from https://auth.dhan.co (rotated daily)
  DHAN_CLIENT_ID      — Dhan client ID (1111957145 for this account)
  DHAN_RPS            — API rate limit (default 4)

Live data flow:
  1. get_ohlc()  -> POST /v2/charts/intraday  (BankNifty futures/index)
  2. get_tick()  -> POST /v2/marketfeed/quote  (real-time single-shot quote)
  3. get_option_chain() -> POST /v2/optionchain (nearest-weekly ATM±12 strikes)

Token renewal:
  Call renew_token() from a background thread every 20 hours to extend
  the JWT without full TOTP re-auth. Or deploy dhan_totp_auth.py as a cron.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import redis
from fastapi import HTTPException

try:
    from contracts_app import get_redis_key, isoformat_ist, parse_timestamp_to_ist, TimestampSourceMode
except ImportError:
    def get_redis_key(k): return k
    def isoformat_ist(dt): return dt.isoformat()
    parse_timestamp_to_ist = None
    TimestampSourceMode = None

from .dhan_client import (
    DhanLiveClient,
    ScripMaster,
    IDX_BANKNIFTY,
    IDX_VIX,
    _parse_intraday_response,
    _to_float,
    _to_int,
)
from .dhan_ws_feed import DhanWsFeed

log = logging.getLogger("dhan_data_service")

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _iso_now_ist() -> str:
    return isoformat_ist(_now_ist())


class DhanDataService:
    """
    Implements the same interface as KiteDataService, powered by Dhan APIs.
    Swap into api_service.py FastAPI handlers transparently.
    """

    def __init__(self) -> None:
        token = str(os.getenv("DHAN_ACCESS_TOKEN") or "").strip()
        client_id = str(os.getenv("DHAN_CLIENT_ID") or "").strip()
        rps = float(os.getenv("DHAN_RPS") or "4")

        self._client = DhanLiveClient(token=token, client_id=client_id, rps=rps)
        self._scrip: Optional[ScripMaster] = None
        self._scrip_lock = threading.Lock()

        from .env_settings import redis_config
        self.redis_client = redis.Redis(**redis_config(decode_responses=True))

        # WebSocket live feed (futures + VIX ticks; starts only if DHAN_WS_ENABLED != 0)
        self._ws_feed: Optional[DhanWsFeed] = None
        if DhanWsFeed.should_enable():
            try:
                fut_sid = self._futures_security_id("BANKNIFTY")
                self._ws_feed = DhanWsFeed(
                    client_id=client_id,
                    access_token=token,
                    futures_security_id=fut_sid,
                    redis_client=self.redis_client,
                )
                self._ws_feed.start()
            except Exception as _ws_err:
                log.warning("DhanWsFeed init failed (falling back to REST poll): %s", _ws_err)
                self._ws_feed = None

        # Token auto-renewal (every 20 hours in background)
        self._start_token_renewal()

    def _start_token_renewal(self) -> None:
        def _renew_loop():
            while True:
                time.sleep(20 * 3600)
                try:
                    new_token = self._client.renew_token()
                    if new_token and self._ws_feed is not None:
                        self._ws_feed.update_token(new_token)
                except Exception as e:
                    log.warning("Token renewal failed: %s", e)

        t = threading.Thread(target=_renew_loop, name="dhan-token-renew", daemon=True)
        t.start()

    def _scrip_master(self) -> ScripMaster:
        if self._scrip is None:
            with self._scrip_lock:
                if self._scrip is None:
                    self._scrip = ScripMaster.load()
        return self._scrip

    def _futures_security_id(self, underlying: str = "BANKNIFTY") -> str:
        """Look up nearest-expiry futures securityId from scrip master."""
        row = self._scrip_master().find_nearest_futures(underlying)
        if row:
            sid = str(row.get("SEM_SMST_SECURITY_ID") or "").strip()
            if sid:
                return sid
        # Hardcoded fallback — BankNifty Jun 2026 (62326). Update if expired.
        log.warning("Futures securityId lookup failed for %s, using hardcoded fallback", underlying)
        return "62326"

    # ── Health check ─────────────────────────────────────────────────────────

    def health_payload(self) -> Dict[str, Any]:
        redis_status = "ok"
        dhan_status  = "ok"
        detail = None
        try:
            self.redis_client.ping()
        except Exception as e:
            redis_status = "error"
            detail = f"redis: {e}"
        try:
            ok = self._client.validate_token()
            if not ok:
                dhan_status = "error"
                detail = (detail or "") + " dhan: token invalid"
        except Exception as e:
            dhan_status = "error"
            detail = (detail or "") + f" dhan: {e}"

        status = "healthy" if redis_status == "ok" and dhan_status == "ok" else "degraded"
        return {
            "status": status,
            "module": "ingestion_app",
            "timestamp": _iso_now_ist(),
            "mode": str(os.getenv("EXECUTION_MODE") or "live").lower(),
            "redis_status": redis_status,
            "dhan_status": dhan_status,
            "detail": detail,
        }

    def system_mode_payload(self) -> Dict[str, Any]:
        mode = str(os.getenv("EXECUTION_MODE") or "live").strip().lower() or "live"
        if mode not in {"live", "historical", "paper"}:
            mode = "unknown"
        return {"mode": mode, "timestamp": _iso_now_ist(), "source": "ingestion_app"}

    # ── get_tick ──────────────────────────────────────────────────────────────

    def get_tick(self, instrument: str) -> Dict[str, Any]:
        """
        Fetch real-time quote for an instrument via /v2/marketfeed/quote.
        Returns same shape as KiteDataService.get_tick().
        """
        symbol_u = str(instrument or "").strip().upper()
        is_vix = "VIX" in symbol_u or "INDIAVIX" in symbol_u.replace(" ", "")

        if is_vix:
            securities = [{"exchangeSegment": "IDX_I", "securityId": IDX_VIX, "instrument": "INDEX"}]
        elif symbol_u.endswith("FUT") or "FUT" in symbol_u:
            # Current-expiry futures
            sid = self._futures_security_id("BANKNIFTY")
            securities = [{"exchangeSegment": "NSE_FNO", "securityId": sid, "instrument": "FUTIDX"}]
        else:
            # Fall back to BankNifty index
            securities = [{"exchangeSegment": "IDX_I", "securityId": IDX_BANKNIFTY, "instrument": "INDEX"}]

        # WS cache-first: if the live feed is healthy, return the cached tick
        safe_key_lookup = symbol_u.replace(" ", "")
        ws_redis_key = f"websocket:tick:{safe_key_lookup}:latest"
        if self._ws_feed is not None:
            cached = self._ws_feed.get_cached_tick(ws_redis_key)
            if cached is not None:
                return cached

        try:
            quotes = self._client.get_quotes(securities)
        except Exception as e:
            log.warning("get_tick failed for %s: %s", instrument, e)
            return {"instrument": symbol_u, "timestamp": _iso_now_ist(), "last_price": float("nan")}

        quote = quotes[0] if quotes else {}
        last_price = _to_float(quote.get("last_price") or quote.get("LTP"))
        oi         = _to_int(quote.get("oi") or quote.get("OI"))

        out = {
            "instrument": symbol_u,
            "timestamp":  _iso_now_ist(),
            "last_price": last_price,
            "last_quantity": None,
            "best_bid": _to_float(quote.get("bid_price")),
            "best_ask": _to_float(quote.get("ask_price")),
            "mid": None,
            "volume": _to_int(quote.get("volume")),
            "oi": oi,
        }
        if math.isfinite(out["best_bid"]) and math.isfinite(out["best_ask"]):
            out["mid"] = (out["best_bid"] + out["best_ask"]) / 2.0

        # Publish to Redis (same key as Kite path — snapshot_app reads from here)
        safe_key = symbol_u.replace(" ", "")
        try:
            self.redis_client.set(
                get_redis_key(f"websocket:tick:{safe_key}:latest"),
                json.dumps(out, default=str),
            )
        except Exception:
            pass
        return out

    # ── get_ohlc ─────────────────────────────────────────────────────────────

    def get_ohlc(self, instrument: str, timeframe: str, limit: int, order: str) -> List[Dict[str, Any]]:
        """
        Fetch OHLC history for an instrument.
        Returns same shape as KiteDataService.get_ohlc() — list of bar dicts with start_at.
        """
        symbol_u = str(instrument or "").strip().upper()
        tf, minutes = _parse_interval(timeframe)
        days_lookback = max(3, int(math.ceil((limit * max(1, minutes)) / 375.0)) + 3)
        now  = _now_ist()
        from_dt = now - timedelta(days=days_lookback)

        is_vix = "VIX" in symbol_u
        is_fut = symbol_u.endswith("FUT") or "FUT" in symbol_u

        if is_vix:
            seg, sid, inst_type = "IDX_I", IDX_VIX, "INDEX"
        elif is_fut:
            sid = self._futures_security_id("BANKNIFTY")
            seg, inst_type = "NSE_FNO", "FUTIDX"
        else:
            seg, sid, inst_type = "IDX_I", IDX_BANKNIFTY, "INDEX"

        try:
            bars = self._client.get_intraday_ohlc(
                security_id=sid,
                exchange_segment=seg,
                instrument=inst_type,
                from_dt=from_dt,
                to_dt=now,
                interval=1,
            )
        except Exception as e:
            log.error("get_ohlc failed for %s: %s", instrument, e)
            raise HTTPException(status_code=502, detail=f"Dhan intraday fetch failed: {e}")

        # Normalize — add instrument field
        normalized = []
        for bar in bars:
            bar["instrument"] = symbol_u
            bar["timeframe"]  = "1m"
            normalized.append(bar)

        normalized.sort(key=lambda x: str(x.get("start_at") or ""))
        if limit > 0:
            normalized = normalized[-limit:]

        # Aggregate if multi-minute timeframe
        if minutes > 1:
            normalized = _aggregate_bars(normalized, minutes)

        if str(order or "asc").strip().lower() == "desc":
            normalized = list(reversed(normalized))

        for bar in normalized:
            bar["timeframe"] = timeframe

        # Publish to Redis sorted set (same format as Kite path)
        raw_key = get_redis_key(f"ohlc_sorted:{symbol_u}:1m")
        try:
            pipe = self.redis_client.pipeline()
            for bar in normalized:
                ts_str = str(bar.get("start_at") or "")
                try:
                    if parse_timestamp_to_ist:
                        dt = parse_timestamp_to_ist(ts_str, naive_mode=TimestampSourceMode.MARKET_IST) or _now_ist()
                    else:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    score = dt.timestamp()
                except Exception:
                    score = time.time()
                pipe.zadd(raw_key, {json.dumps(bar, default=str): score})
            pipe.execute()
            size = int(self.redis_client.zcard(raw_key) or 0)
            if size > 2400:
                self.redis_client.zremrangebyrank(raw_key, 0, size - 2401)
        except Exception:
            pass

        return normalized

    # ── get_option_chain ─────────────────────────────────────────────────────

    def get_depth(self, instrument: str) -> Dict[str, Any]:
        """Market depth — Dhan provides up to 5 levels. Returns empty if unavailable."""
        symbol_u = str(instrument or "").strip().upper()
        return {
            "instrument": symbol_u,
            "timestamp": _iso_now_ist(),
            "buy": [],
            "sell": [],
            "status": "depth_not_available_via_dhan_rest",
        }

    def list_instruments(self) -> List[Dict[str, Any]]:
        from .env_settings import resolve_instrument_symbol
        configured = str(resolve_instrument_symbol() or "").strip().upper()
        out = []
        if configured and configured != "INSTRUMENT_NOT_SET":
            out.append({"symbol": configured, "exchange": "NSE_FNO"})
        out.append({"symbol": "INDIA VIX", "exchange": "IDX_I"})
        return out

    # Alias — api_service routes call get_options_chain (with 's')
    def get_options_chain(self, instrument: str) -> Dict[str, Any]:
        return self.get_option_chain(instrument)

    def get_option_chain(
        self, instrument: str, strike_span: int = 12
    ) -> Dict[str, Any]:
        """
        Fetch option chain from Dhan /v2/optionchain.
        Returns same shape as KiteDataService option chain — dict with 'strikes', 'expiry',
        'spot', 'pcr', 'max_pain', 'timestamp'.
        """
        underlying_u = _extract_underlying(instrument)
        if not underlying_u:
            raise HTTPException(status_code=400, detail=f"cannot infer underlying from {instrument}")

        try:
            resp = self._client.get_option_chain(underlying_u)
        except Exception as e:
            log.error("get_option_chain failed: %s", e)
            raise HTTPException(status_code=502, detail=f"Dhan optionchain failed: {e}")

        return _parse_option_chain(resp, underlying_u, strike_span)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_underlying(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace(" ", "")
    if s in ("BANKNIFTY", "NIFTYBANK"):
        return "BANKNIFTY"
    if s in ("NIFTY", "NIFTY50"):
        return "NIFTY"
    if s.startswith("BANKNIFTY"):
        return "BANKNIFTY"
    if s.startswith("NIFTY"):
        return "NIFTY"
    return s


def _parse_interval(timeframe: str) -> Tuple[str, int]:
    tf = str(timeframe or "1m").strip().lower()
    mapping = {
        "minute": ("minute", 1), "1m": ("minute", 1), "1min": ("minute", 1),
        "3m": ("3minute", 3), "5m": ("5minute", 5), "10m": ("10minute", 10),
        "15m": ("15minute", 15), "30m": ("30minute", 30), "60m": ("60minute", 60), "1h": ("60minute", 60),
    }
    return mapping.get(tf, ("minute", 1))


def _aggregate_bars(bars: List[Dict[str, Any]], minutes: int) -> List[Dict[str, Any]]:
    if minutes <= 1 or not bars:
        return bars
    out: List[Dict[str, Any]] = []
    bucket: List[Dict[str, Any]] = []
    bucket_start = None
    for bar in bars:
        raw_ts = bar.get("start_at")
        try:
            dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            dt = dt.astimezone(IST)
        except Exception:
            continue
        floored = (dt.minute // minutes) * minutes
        current = dt.replace(minute=floored, second=0, microsecond=0)
        if bucket_start is None or current != bucket_start:
            if bucket:
                out.append(_merge_bucket(bucket))
            bucket = []
            bucket_start = current
        bucket.append(bar)
    if bucket:
        out.append(_merge_bucket(bucket))
    return out


def _merge_bucket(bucket: List[Dict[str, Any]]) -> Dict[str, Any]:
    first, last = bucket[0], bucket[-1]
    highs   = [_to_float(x.get("high"))   for x in bucket]
    lows    = [_to_float(x.get("low"))    for x in bucket]
    volumes = [max(0, _to_float(x.get("volume"))) for x in bucket]
    return {
        "instrument": first.get("instrument"),
        "timeframe":  first.get("timeframe"),
        "open":       _to_float(first.get("open")),
        "high":       max([x for x in highs if math.isfinite(x)] or [float("nan")]),
        "low":        min([x for x in lows  if math.isfinite(x)] or [float("nan")]),
        "close":      _to_float(last.get("close")),
        "volume":     int(sum(volumes)),
        "oi":         _to_int(last.get("oi")),
        "start_at":   first.get("start_at"),
    }


def _parse_option_chain(
    resp: Dict[str, Any], underlying: str, strike_span: int
) -> Dict[str, Any]:
    """
    Parse Dhan /v2/optionchain response into the same structure
    KiteDataService returns so snapshot_app doesn't need to change.

    Dhan response shape (typical):
    {
      "data": {
        "underlyingDetails": {"last_price": 58400.0, "change": ...},
        "optionData": [
          {
            "strike_price": 58400.0,
            "call": {"last_price": 200.0, "oi": 50000, "iv": 12.5, "volume": 100, ...},
            "put":  {"last_price": 180.0, "oi": 60000, "iv": 11.0, "volume": 90,  ...},
          }, ...
        ],
        "expiryDate": "2026-06-25"
      }
    }
    """
    data = (resp or {}).get("data") or resp or {}

    # Underlying spot price
    underlying_info = data.get("underlyingDetails") or {}
    spot = _to_float(underlying_info.get("last_price") or underlying_info.get("LTP"))

    expiry = str(data.get("expiryDate") or "")

    option_data = data.get("optionData") or data.get("oc") or []

    strikes: List[Dict[str, Any]] = []
    total_ce_oi = 0.0
    total_pe_oi = 0.0

    for row in option_data:
        strike = _to_float(row.get("strike_price") or row.get("strikePrice"))
        if not math.isfinite(strike):
            continue

        call = row.get("call") or row.get("ce") or {}
        put  = row.get("put")  or row.get("pe") or {}

        ce_ltp    = _to_float(call.get("last_price") or call.get("LTP"))
        ce_oi     = _to_float(call.get("oi") or call.get("OI")) or 0.0
        ce_volume = _to_float(call.get("volume")) or 0.0
        ce_iv     = _to_float(call.get("iv") or call.get("impliedVolatility"))
        ce_bid    = _to_float(call.get("bid_price"))
        ce_ask    = _to_float(call.get("ask_price"))

        pe_ltp    = _to_float(put.get("last_price") or put.get("LTP"))
        pe_oi     = _to_float(put.get("oi") or put.get("OI")) or 0.0
        pe_volume = _to_float(put.get("volume")) or 0.0
        pe_iv     = _to_float(put.get("iv") or put.get("impliedVolatility"))
        pe_bid    = _to_float(put.get("bid_price"))
        pe_ask    = _to_float(put.get("ask_price"))

        total_ce_oi += ce_oi
        total_pe_oi += pe_oi

        strikes.append({
            "strike":    int(round(strike)),
            "ce_ltp":   ce_ltp,
            "ce_oi":    _to_int(ce_oi),
            "ce_volume":_to_int(ce_volume),
            "ce_iv":    ce_iv,
            "ce_bid":   ce_bid,
            "ce_ask":   ce_ask,
            "pe_ltp":   pe_ltp,
            "pe_oi":    _to_int(pe_oi),
            "pe_volume":_to_int(pe_volume),
            "pe_iv":    pe_iv,
            "pe_bid":   pe_bid,
            "pe_ask":   pe_ask,
        })

    pcr = float(total_pe_oi / total_ce_oi) if total_ce_oi > 0 else None
    max_pain = _compute_max_pain(strikes)

    return {
        "underlying": underlying,
        "spot": spot,
        "expiry": expiry,
        "timestamp": _now_ist().isoformat(),
        "strikes": strikes,
        "pcr": pcr,
        "max_pain": max_pain,
        "total_ce_oi": _to_int(total_ce_oi),
        "total_pe_oi": _to_int(total_pe_oi),
    }


def _compute_max_pain(strikes: List[Dict[str, Any]]) -> Optional[int]:
    if not strikes:
        return None
    best_strike = None
    min_payout = float("inf")
    for row in strikes:
        s = _to_float(row.get("strike"))
        if not math.isfinite(s):
            continue
        payout = 0.0
        for other in strikes:
            s2 = _to_float(other.get("strike"))
            if not math.isfinite(s2):
                continue
            ce_oi = float(other.get("ce_oi") or 0)
            pe_oi = float(other.get("pe_oi") or 0)
            if s2 < s:
                payout += (s - s2) * ce_oi
            elif s2 > s:
                payout += (s2 - s) * pe_oi
        if payout < min_payout:
            min_payout = payout
            best_strike = int(round(s))
    return best_strike
