from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import redis
import uvicorn

from redis_key_manager import get_redis_key

from .env_settings import credentials_path_candidates, redis_config, resolve_instrument_symbol
from .kite_client import create_kite_client


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
    return _now_ist().isoformat()


def _extract_underlying(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    for suffix in ("FUT", "CE", "PE"):
        if suffix in raw:
            left = raw.split(suffix)[0]
            out = "".join(ch for ch in left if ch.isalpha())
            if out:
                return out
    if raw in {"NIFTY BANK", "BANKNIFTY", "NIFTY", "NIFTY 50"}:
        return "BANKNIFTY" if "BANK" in raw else "NIFTY"
    out = "".join(ch for ch in raw if ch.isalpha())
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
        out = {
            "instrument": str(instrument or "").strip().upper(),
            "timestamp": _iso_now_ist(),
            "last_price": _to_float(quote.get("last_price")),
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
                    "start_at": dt.astimezone(IST).replace(tzinfo=None).isoformat(),
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
                ts = datetime.fromisoformat(str(row["start_at"])).replace(tzinfo=IST).timestamp()
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


svc = KiteDataService()
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
