from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import redis
import uvicorn

from contracts_app import TimestampSourceMode, get_redis_key, isoformat_ist, parse_timestamp_to_ist

from .env_settings import credentials_path_candidates, redis_config, resolve_instrument_symbol

log = logging.getLogger("ingestion_api")
from .kite_client import create_kite_client
from .strike_ohlc import StrikeOhlcAccumulator


IST = timezone(timedelta(hours=5, minutes=30))


def _to_float(value: Any) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _to_int(value: Any) -> Optional[int]:
    num = _to_float(value)
    if math.isfinite(num):
        return int(round(num))
    return None


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _iso_now_ist() -> str:
    return isoformat_ist(_now_ist())


def _extract_underlying(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if ":" in raw:
        raw = raw.split(":", 1)[1].strip().upper()
    compact = raw.replace(" ", "")
    if compact in {"NIFTYBANK", "BANKNIFTY"}:
        return "BANKNIFTY"
    if compact in {"NIFTY", "NIFTY50"}:
        return "NIFTY"
    match = re.match(r"^([A-Z]+)\d", compact)
    if match:
        return str(match.group(1) or "").strip().upper()
    for suffix in ("FUT", "CE", "PE"):
        if compact.endswith(suffix):
            left = compact[: -len(suffix)]
            out = "".join(ch for ch in left if ch.isalpha())
            if out:
                return out
    out = "".join(ch for ch in compact if ch.isalpha())
    return out


def _is_fno_symbol(symbol: str) -> bool:
    s = str(symbol or "").upper()
    return any(tag in s for tag in ("FUT", "CE", "PE"))


def _resolve_exchange(symbol: str) -> str:
    if ":" in str(symbol):
        return str(symbol).split(":", 1)[0].strip().upper() or "NSE"
    return "NFO" if _is_fno_symbol(symbol) else "NSE"


def _normalize_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip()
    if ":" in raw:
        return raw
    exchange = _resolve_exchange(raw)
    if exchange == "NSE" and raw.upper() == "BANKNIFTY":
        return "NSE:NIFTY BANK"
    if exchange == "NSE" and raw.upper() == "NIFTY":
        return "NSE:NIFTY 50"
    if exchange == "NSE" and raw.upper() in {"INDIAVIX", "INDIA VIX"}:
        return "NSE:INDIA VIX"
    return f"{exchange}:{raw}"


def _load_kite_credentials() -> Tuple[Optional[str], Optional[str]]:
    api_key = str(os.getenv("KITE_API_KEY") or "").strip()
    access_token = str(os.getenv("KITE_ACCESS_TOKEN") or "").strip()
    if api_key and access_token:
        return api_key, access_token

    for path in credentials_path_candidates():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        key = str(payload.get("api_key") or "").strip()
        token = str(
            payload.get("access_token")
            or ((payload.get("data") or {}).get("access_token") if isinstance(payload.get("data"), dict) else "")
            or ""
        ).strip()
        if key and token:
            return key, token
    return None, None


def _pcr_and_max_pain(strikes: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[int]]:
    total_ce = 0.0
    total_pe = 0.0
    for row in strikes:
        total_ce += max(0.0, _to_float(row.get("ce_oi")))
        total_pe += max(0.0, _to_float(row.get("pe_oi")))
    pcr = float(total_pe / total_ce) if total_ce > 0 else None

    best_strike: Optional[int] = None
    min_payout = float("inf")
    for row in strikes:
        strike = _to_float(row.get("strike"))
        if not math.isfinite(strike):
            continue
        payout = 0.0
        for other in strikes:
            s2 = _to_float(other.get("strike"))
            if not math.isfinite(s2):
                continue
            ce_oi = max(0.0, _to_float(other.get("ce_oi")))
            pe_oi = max(0.0, _to_float(other.get("pe_oi")))
            if s2 < strike:
                payout += (strike - s2) * ce_oi
            elif s2 > strike:
                payout += (s2 - strike) * pe_oi
        if payout < min_payout:
            min_payout = payout
            best_strike = int(round(strike))
    return pcr, best_strike


def _parse_interval(timeframe: str) -> Tuple[str, int]:
    tf = str(timeframe or "1m").strip().lower()
    if tf in {"minute", "1m", "1min"}:
        return "minute", 1
    if tf in {"3m", "3min"}:
        return "3minute", 3
    if tf in {"5m", "5min"}:
        return "5minute", 5
    if tf in {"10m", "10min"}:
        return "10minute", 10
    if tf in {"15m", "15min"}:
        return "15minute", 15
    if tf in {"30m", "30min"}:
        return "30minute", 30
    if tf in {"60m", "60min", "1h"}:
        return "60minute", 60
    return "minute", 1


@dataclass
class _CachedInstruments:
    rows: List[Dict[str, Any]]
    loaded_at: float


class KiteDataService:
    def __init__(self) -> None:
        self.redis_client = redis.Redis(**redis_config(decode_responses=True))
        self._kite = None
        self._kite_lock = threading.Lock()
        self._ins_cache: Dict[str, _CachedInstruments] = {}
        self._ins_ttl_sec = max(60, int(os.getenv("KITE_INSTRUMENTS_TTL_SECONDS", "900")))
        # Per-strike 1-min OHLC sampler (forward exit-fidelity fix). Default OFF — the live
        # data path is unchanged until STRIKE_OHLC_ENABLED is set. Builds intrabar option
        # OHLC by sampling last_price every few seconds (Kite quote() only gives day-OHLC).
        self._strike_ohlc = StrikeOhlcAccumulator()
        self._strike_ohlc_enabled = os.getenv("STRIKE_OHLC_ENABLED", "false").strip().lower() in ("1", "true", "yes")
        self._strike_ohlc_instruments = [s.strip().upper() for s in
                                         os.getenv("STRIKE_OHLC_INSTRUMENTS", "BANKNIFTY").split(",") if s.strip()]
        self._strike_ohlc_sample_sec = max(2, int(os.getenv("STRIKE_OHLC_SAMPLE_SECONDS", "5")))
        self._strike_sampler_started = False
        self._strike_sampler_lock = threading.Lock()

    def _kite_client(self):
        if self._kite is not None:
            return self._kite
        with self._kite_lock:
            if self._kite is not None:
                return self._kite
            api_key, access_token = _load_kite_credentials()
            if not api_key or not access_token:
                raise RuntimeError("Kite credentials unavailable")
            self._kite = create_kite_client(api_key=api_key, access_token=access_token)
            return self._kite

    # ---- per-strike 1-min OHLC sampler (forward exit-fidelity fix) ----

    def _ensure_strike_sampler(self) -> None:
        """Lazily start the background sampler thread once (only when enabled)."""
        if not self._strike_ohlc_enabled or self._strike_sampler_started:
            return
        with self._strike_sampler_lock:
            if self._strike_sampler_started:
                return
            threading.Thread(target=self._strike_ohlc_loop, name="strike-ohlc", daemon=True).start()
            self._strike_sampler_started = True

    def _strike_ohlc_loop(self) -> None:
        while True:
            for inst in self._strike_ohlc_instruments:
                try:
                    self._sample_strike_ohlc(inst)
                except Exception:
                    pass
            try:
                self._strike_ohlc.prune(time.time() - 300)
            except Exception:
                pass
            time.sleep(self._strike_ohlc_sample_sec)

    def _sample_strike_ohlc(self, instrument: str) -> None:
        _expiry, rows, _fut = self._select_options(instrument=instrument)
        symbols = [f"NFO:{r.get('tradingsymbol')}" for r in rows if r.get("tradingsymbol")]
        if not symbols:
            return
        quote_map = self._kite_client().quote(symbols)
        now = time.time()
        for r in rows:
            strike = _to_float(r.get("strike"))
            side = str(r.get("instrument_type") or "").upper()
            if strike is None or side not in ("CE", "PE"):
                continue
            q = quote_map.get(f"NFO:{r.get('tradingsymbol')}") or {}
            self._strike_ohlc.update(int(round(strike)), side, _to_float(q.get("last_price")), now)

    def health_payload(self) -> Dict[str, Any]:
        status = "healthy"
        kite_status = "ok"
        redis_status = "ok"
        detail = None
        try:
            self.redis_client.ping()
        except Exception as exc:
            redis_status = "error"
            status = "degraded"
            detail = f"redis_error: {exc}"

        try:
            client = self._kite_client()
            _ = client.profile()
        except Exception as exc:
            kite_status = "error"
            status = "degraded"
            detail = detail or f"kite_error: {exc}"

        return {
            "status": status,
            "module": "ingestion_app",
            "timestamp": _iso_now_ist(),
            "mode": str(os.getenv("EXECUTION_MODE") or "live").lower(),
            "redis_status": redis_status,
            "kite_status": kite_status,
            "detail": detail,
        }

    def system_mode_payload(self) -> Dict[str, Any]:
        mode = str(os.getenv("EXECUTION_MODE") or "live").strip().lower() or "live"
        if mode not in {"live", "historical", "paper"}:
            mode = "unknown"
        return {
            "mode": mode,
            "timestamp": _iso_now_ist(),
            "source": "ingestion_app",
        }

    def _load_instruments(self, exchange: str) -> List[Dict[str, Any]]:
        key = str(exchange).upper()
        cached = self._ins_cache.get(key)
        now = time.time()
        if cached is not None and (now - cached.loaded_at) < self._ins_ttl_sec:
            return cached.rows

        rows = self._kite_client().instruments(exchange=key)
        if not isinstance(rows, list):
            rows = []
        self._ins_cache[key] = _CachedInstruments(rows=rows, loaded_at=now)
        return rows

    def _resolve_instrument_token(self, instrument: str) -> Optional[int]:
        symbol = str(instrument or "").strip().upper()
        if not symbol:
            return None
        exchange = _resolve_exchange(symbol)
        rows = self._load_instruments(exchange)
        for row in rows:
            if str(row.get("tradingsymbol") or "").strip().upper() == symbol:
                token = row.get("instrument_token")
                if str(token).isdigit():
                    return int(token)
        return None

    def get_tick(self, instrument: str) -> Dict[str, Any]:
        symbol = _normalize_symbol(instrument)
        quote = self._kite_client().quote([symbol]).get(symbol) or {}
        last_price = _to_float(quote.get("last_price"))
        depth = quote.get("depth") if isinstance(quote.get("depth"), dict) else {}
        buy_depth = depth.get("buy") or []
        sell_depth = depth.get("sell") or []
        best_bid = _to_float(buy_depth[0].get("price")) if buy_depth else None
        best_ask = _to_float(sell_depth[0].get("price")) if sell_depth else None
        mid = (
            float((best_bid + best_ask) / 2.0)
            if best_bid is not None and best_ask is not None
            else None
        )
        last_quantity = _to_int(quote.get("last_quantity") or quote.get("last_traded_quantity"))
        out = {
            "instrument": str(instrument or "").strip().upper(),
            "timestamp": _iso_now_ist(),
            "last_price": last_price,
            "last_quantity": last_quantity,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "volume": _to_int(quote.get("volume")),
            "oi": _to_int(quote.get("oi")),
            "oi_day_high": _to_int(quote.get("oi_day_high")),
            "oi_day_low": _to_int(quote.get("oi_day_low")),
        }
        safe_key = out["instrument"].replace(" ", "")
        try:
            self.redis_client.set(get_redis_key(f"websocket:tick:{safe_key}:latest"), json.dumps(out, default=str))
        except Exception:
            pass
        return out

    def _aggregate_rows(self, rows: List[Dict[str, Any]], timeframe_minutes: int) -> List[Dict[str, Any]]:
        if timeframe_minutes <= 1 or not rows:
            return rows
        out: List[Dict[str, Any]] = []
        bucket: List[Dict[str, Any]] = []
        bucket_start: Optional[datetime] = None

        for row in rows:
            raw_ts = row.get("start_at")
            try:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            ts_ist = ts.astimezone(IST)
            floored_minute = (ts_ist.minute // timeframe_minutes) * timeframe_minutes
            current_bucket = ts_ist.replace(minute=floored_minute, second=0, microsecond=0)
            if bucket_start is None or current_bucket != bucket_start:
                if bucket:
                    out.append(self._merge_bucket(bucket))
                bucket = []
                bucket_start = current_bucket
            bucket.append(row)
        if bucket:
            out.append(self._merge_bucket(bucket))
        return out

    def _merge_bucket(self, bucket: List[Dict[str, Any]]) -> Dict[str, Any]:
        first = bucket[0]
        last = bucket[-1]
        highs = [_to_float(x.get("high")) for x in bucket]
        lows = [_to_float(x.get("low")) for x in bucket]
        volumes = [max(0, _to_float(x.get("volume"))) for x in bucket]
        return {
            "instrument": first.get("instrument"),
            "timeframe": first.get("timeframe"),
            "open": _to_float(first.get("open")),
            "high": max([x for x in highs if math.isfinite(x)] or [_to_float(first.get("high"))]),
            "low": min([x for x in lows if math.isfinite(x)] or [_to_float(first.get("low"))]),
            "close": _to_float(last.get("close")),
            "volume": int(sum(volumes)),
            "oi": _to_int(last.get("oi")),
            "start_at": first.get("start_at"),
        }

    def get_ohlc(self, instrument: str, timeframe: str, limit: int, order: str) -> List[Dict[str, Any]]:
        token = self._resolve_instrument_token(instrument)
        if token is None:
            raise HTTPException(status_code=404, detail=f"instrument token not found for {instrument}")

        interval, minutes = _parse_interval(timeframe)
        days_lookback = max(2, int(math.ceil((limit * max(1, minutes)) / 375.0)) + 2)
        now_ist = _now_ist()
        from_dt = now_ist - timedelta(days=days_lookback)
        continuous = str(instrument or "").upper().endswith("FUT")

        client = self._kite_client()
        try:
            rows = client.historical_data(
                instrument_token=token,
                from_date=from_dt,
                to_date=now_ist,
                interval="minute",
                continuous=continuous,
                oi=True,
            )
        except Exception:
            rows = client.historical_data(
                instrument_token=token,
                from_date=from_dt,
                to_date=now_ist,
                interval="minute",
                continuous=False,
                oi=True,
            )
        if not isinstance(rows, list):
            rows = []

        # Rollover guard: Kite's continuous=True futures series can return volume=0 for the
        # new front-month right after an expiry roll (price + OI come through, volume does not).
        # When that happens, refetch the SPECIFIC contract (continuous=False) which carries
        # real per-bar volume. No-op unless volume is entirely missing AND the alt actually
        # has volume — so the working path (volume present) is never disturbed.
        if continuous and rows and not any((_to_float(r.get("volume")) or 0) > 0 for r in rows):
            try:
                alt = client.historical_data(
                    instrument_token=token,
                    from_date=from_dt,
                    to_date=now_ist,
                    interval="minute",
                    continuous=False,
                    oi=True,
                )
                if isinstance(alt, list) and any((_to_float(r.get("volume")) or 0) > 0 for r in alt):
                    rows = alt
            except Exception:
                pass

        normalized: List[Dict[str, Any]] = []
        symbol_u = str(instrument).strip().upper()
        for row in rows:
            raw_dt = row.get("date")
            if raw_dt is None:
                continue
            if isinstance(raw_dt, datetime):
                dt = raw_dt
            else:
                try:
                    dt = datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
                except Exception:
                    continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            normalized.append(
                {
                    "instrument": symbol_u,
                    "timeframe": "1m",
                    "open": _to_float(row.get("open")),
                    "high": _to_float(row.get("high")),
                    "low": _to_float(row.get("low")),
                    "close": _to_float(row.get("close")),
                    "volume": _to_int(row.get("volume")) or 0,
                    "oi": _to_int(row.get("oi")),
                    "start_at": isoformat_ist(dt.astimezone(IST)),
                }
            )

        normalized.sort(key=lambda x: str(x.get("start_at") or ""))
        if limit > 0:
            normalized = normalized[-limit:]

        tf_rows = self._aggregate_rows(normalized, timeframe_minutes=minutes)
        if str(order or "asc").strip().lower() == "desc":
            tf_rows = list(reversed(tf_rows))

        key = get_redis_key(f"ohlc_sorted:{symbol_u}:1m")
        try:
            pipe = self.redis_client.pipeline()
            for row in normalized[-min(len(normalized), max(1, limit)) :]:
                ts = (parse_timestamp_to_ist(row["start_at"], naive_mode=TimestampSourceMode.MARKET_IST) or _now_ist()).timestamp()
                pipe.zadd(key, {json.dumps(row, default=str): ts})
            pipe.execute()
            size = int(self.redis_client.zcard(key) or 0)
            overflow = size - 2400
            if overflow > 0:
                self.redis_client.zremrangebyrank(key, 0, overflow - 1)
        except Exception:
            pass

        for row in tf_rows:
            row["timeframe"] = timeframe
        return tf_rows

    def _select_options(self, instrument: str, strike_span: int = 12) -> Tuple[str, List[Dict[str, Any]], float]:
        symbol_u = str(instrument or "").strip().upper()
        underlying = _extract_underlying(symbol_u)
        if not underlying:
            raise HTTPException(status_code=400, detail=f"could not infer underlying from {instrument}")

        nfo_rows = self._load_instruments("NFO")
        option_rows = [
            row
            for row in nfo_rows
            if str(row.get("name") or "").upper() == underlying
            and str(row.get("instrument_type") or "").upper() in {"CE", "PE"}
        ]
        if not option_rows:
            raise HTTPException(status_code=404, detail=f"no options instruments found for underlying={underlying}")

        now_date = _now_ist().date()
        expiries: List[datetime.date] = []
        for row in option_rows:
            exp = row.get("expiry")
            try:
                exp_dt = datetime.fromisoformat(str(exp)).date()
            except Exception:
                continue
            expiries.append(exp_dt)
        if not expiries:
            raise HTTPException(status_code=404, detail="no valid expiry in options instruments")

        future_exp = sorted({d for d in expiries if d >= now_date})
        target_expiry = future_exp[0] if future_exp else sorted(set(expiries))[0]

        expiry_rows = []
        for row in option_rows:
            try:
                exp_dt = datetime.fromisoformat(str(row.get("expiry"))).date()
            except Exception:
                continue
            if exp_dt == target_expiry:
                expiry_rows.append(row)
        if not expiry_rows:
            raise HTTPException(status_code=404, detail="no options rows for target expiry")

        fut_symbol = _normalize_symbol(symbol_u)
        fut_quote = self._kite_client().quote([fut_symbol]).get(fut_symbol) or {}
        fut_price = _to_float(fut_quote.get("last_price"))
        if not math.isfinite(fut_price):
            raise HTTPException(status_code=502, detail=f"failed to fetch underlying quote for {instrument}")

        strikes = sorted(
            {
                int(round(_to_float(row.get("strike"))))
                for row in expiry_rows
                if math.isfinite(_to_float(row.get("strike")))
            }
        )
        if not strikes:
            raise HTTPException(status_code=404, detail="no valid strike prices available")
        atm = min(strikes, key=lambda s: abs(s - fut_price))
        allowed = {s for s in strikes if abs(s - atm) <= 100 * strike_span}

        selected = [
            row for row in expiry_rows if int(round(_to_float(row.get("strike")))) in allowed
        ]
        return target_expiry.isoformat(), selected, float(fut_price)

    def get_options_chain(self, instrument: str) -> Dict[str, Any]:
        self._ensure_strike_sampler()
        expiry, rows, fut_price = self._select_options(instrument=instrument)

        quote_symbols = [f"NFO:{row.get('tradingsymbol')}" for row in rows if row.get("tradingsymbol")]
        quote_map = self._kite_client().quote(quote_symbols) if quote_symbols else {}

        by_strike: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            strike = int(round(_to_float(row.get("strike"))))
            side = str(row.get("instrument_type") or "").upper()
            symbol = f"NFO:{row.get('tradingsymbol')}"
            q = quote_map.get(symbol) or {}
            rec = by_strike.setdefault(
                strike,
                {
                    "strike": strike,
                    "CE": {},
                    "PE": {},
                    "ce_ltp": None,
                    "pe_ltp": None,
                    "ce_oi": None,
                    "pe_oi": None,
                    "ce_volume": None,
                    "pe_volume": None,
                },
            )
            side_payload = {
                "symbol": str(row.get("tradingsymbol") or ""),
                "last_price": _to_float(q.get("last_price")),
                "oi": _to_int(q.get("oi")),
                "volume": _to_int(q.get("volume")),
            }
            rec[side] = side_payload
            if side == "CE":
                rec["ce_ltp"] = side_payload["last_price"]
                rec["ce_oi"] = side_payload["oi"]
                rec["ce_volume"] = side_payload["volume"]
            elif side == "PE":
                rec["pe_ltp"] = side_payload["last_price"]
                rec["pe_oi"] = side_payload["oi"]
                rec["pe_volume"] = side_payload["volume"]

        strikes = [by_strike[k] for k in sorted(by_strike.keys())]
        if self._strike_ohlc_enabled:                  # fill intrabar 1-min OHLC from the sampler
            for rec in strikes:
                cb = self._strike_ohlc.bar(rec["strike"], "CE")
                if cb:
                    rec["ce_open"], rec["ce_high"], rec["ce_low"] = cb["open"], cb["high"], cb["low"]
                pb = self._strike_ohlc.bar(rec["strike"], "PE")
                if pb:
                    rec["pe_open"], rec["pe_high"], rec["pe_low"] = pb["open"], pb["high"], pb["low"]
        pcr, max_pain = _pcr_and_max_pain(strikes)
        instrument_u = str(instrument or "").strip().upper()
        payload = {
            "instrument": instrument_u,
            "expiry": expiry,
            "timestamp": _iso_now_ist(),
            "futures_price": fut_price,
            "pcr": pcr,
            "max_pain": max_pain,
            "strikes": strikes,
        }

        try:
            keys = [
                get_redis_key(f"options:{instrument_u}:{expiry}:chain"),
                get_redis_key(f"options:{instrument_u}:chain"),
            ]
            encoded = json.dumps(payload, default=str)
            for key in keys:
                self.redis_client.setex(key, 120, encoded)
        except Exception:
            pass
        return payload

    def get_depth(self, instrument: str) -> Dict[str, Any]:
        symbol = _normalize_symbol(instrument)
        quote = self._kite_client().quote([symbol]).get(symbol) or {}
        depth = quote.get("depth") if isinstance(quote.get("depth"), dict) else {}
        buy = list(depth.get("buy") or [])
        sell = list(depth.get("sell") or [])
        payload = {
            "instrument": str(instrument or "").strip().upper(),
            "timestamp": _iso_now_ist(),
            "buy": buy,
            "sell": sell,
            "status": "ok",
        }
        try:
            safe = payload["instrument"].replace(" ", "")
            self.redis_client.set(get_redis_key(f"depth:{safe}:buy"), json.dumps(buy, default=str))
            self.redis_client.set(get_redis_key(f"depth:{safe}:sell"), json.dumps(sell, default=str))
            self.redis_client.set(get_redis_key(f"depth:{safe}:timestamp"), payload["timestamp"])
        except Exception:
            pass
        return payload

    def list_instruments(self) -> List[Dict[str, Any]]:
        configured = str(resolve_instrument_symbol() or "").strip().upper()
        out: List[Dict[str, Any]] = []
        if configured and configured != "INSTRUMENT_NOT_SET":
            out.append({"symbol": configured, "exchange": _resolve_exchange(configured)})
        out.append({"symbol": "INDIA VIX", "exchange": "NSE"})
        return out


# ── Broker market-data registry ───────────────────────────────────────────────
# The broker is a PLUGGABLE ADAPTER. Swapping Kite/Dhan/Zerodha/any broker must NOT
# require changes to snapshot_app/strategy_app/feature_engine/models/strategy config —
# only this registry + that broker's credentials. Adding a broker = implement the
# MarketDataService interface + add one line to _MARKET_DATA_SERVICES + set BROKER=<name>.
# Mirrors execution_app's EXECUTION_ADAPTER pattern. Selected by the explicit `BROKER`
# env var (preferred); falls back to the legacy DHAN_ACCESS_TOKEN-presence heuristic.
#
# MarketDataService interface (structural — adapters need not subclass, just implement):
#   get_tick(instrument) -> dict          get_ohlc(instrument, timeframe) -> dict
#   get_options_chain(...) / get_option_chain(...) -> dict
#   get_depth(instrument) -> dict         list_instruments() -> list
#   health_payload() -> dict              system_mode_payload() -> dict

def _make_kite_service():
    return KiteDataService()


def _make_dhan_service():
    from .dhan_data_service import DhanDataService
    return DhanDataService()


# name -> factory. New broker: add one line (and its adapter module). Nothing else changes.
_MARKET_DATA_SERVICES = {
    "kite": _make_kite_service,
    "zerodha": _make_kite_service,   # alias (Kite Connect is Zerodha's API)
    "dhan": _make_dhan_service,
}


def _resolve_broker() -> str:
    """Explicit BROKER env wins; else legacy heuristic (Dhan token present -> dhan, else kite)."""
    broker = str(os.getenv("BROKER", "") or "").strip().lower()
    if broker:
        return broker
    if os.getenv("DHAN_ACCESS_TOKEN"):
        return "dhan"
    return "kite"


def _build_svc():
    broker = _resolve_broker()
    factory = _MARKET_DATA_SERVICES.get(broker)
    if factory is None:
        raise ValueError(
            f"Unknown BROKER={broker!r}; known brokers: {sorted(_MARKET_DATA_SERVICES)}. "
            "Add an adapter to _MARKET_DATA_SERVICES to support a new broker."
        )
    log.info("ingestion_app: market-data broker = %s", broker)
    return factory()


svc = _build_svc()
app = FastAPI(title="Ingestion API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return svc.health_payload()


@app.get("/api/v1/market/tick/{instrument}")
async def market_tick(instrument: str) -> Dict[str, Any]:
    return svc.get_tick(instrument)


@app.get("/api/v1/system/mode")
async def system_mode() -> Dict[str, Any]:
    return svc.system_mode_payload()


@app.get("/api/v1/market/ohlc/{instrument}")
async def market_ohlc(
    instrument: str,
    timeframe: str = Query(default="1m"),
    limit: int = Query(default=300, ge=1, le=5000),
    order: str = Query(default="asc"),
) -> List[Dict[str, Any]]:
    return svc.get_ohlc(instrument=instrument, timeframe=timeframe, limit=limit, order=order)


@app.get("/api/v1/ohlc/{instrument}")
async def market_ohlc_alias(
    instrument: str,
    timeframe: str = Query(default="1m"),
    limit: int = Query(default=300, ge=1, le=5000),
    order: str = Query(default="asc"),
) -> List[Dict[str, Any]]:
    return svc.get_ohlc(instrument=instrument, timeframe=timeframe, limit=limit, order=order)


@app.get("/api/v1/options/chain/{instrument}")
async def options_chain(instrument: str) -> Dict[str, Any]:
    return svc.get_options_chain(instrument=instrument)


@app.get("/api/v1/market/depth/{instrument}")
async def market_depth(instrument: str) -> Dict[str, Any]:
    return svc.get_depth(instrument=instrument)


@app.get("/api/v1/market/instruments")
async def market_instruments() -> List[Dict[str, Any]]:
    return svc.list_instruments()


def run() -> None:
    host = str(os.getenv("INGESTION_API_HOST") or "0.0.0.0")
    port = int(os.getenv("INGESTION_API_PORT") or os.getenv("MARKET_DATA_API_PORT") or "8004")
    uvicorn.run(app, host=host, port=port, log_level=str(os.getenv("LOG_LEVEL") or "info").lower())


if __name__ == "__main__":
    run()
