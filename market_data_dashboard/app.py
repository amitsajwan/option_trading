#!/usr/bin/env python3
"""
Market Data Dashboard - Standalone Status and Visualization

This provides a web interface for monitoring market data status and visualization,
completely decoupled from engine/trading functionality.
"""

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import requests
import json
import asyncio
import math
from datetime import datetime, timezone, timedelta
import os
import logging
from typing import Dict, Any, List, Optional, Sequence, Tuple
import time
import redis
import uuid
import threading
import queue
import fnmatch
import subprocess
import sys
from collections import deque
from urllib.parse import urlencode

try:
    import sys
    # Ensure market_data src is importable for shared option math helpers
    MARKET_DATA_SRC = Path(__file__).parent.parent / "market_data" / "src"
    if MARKET_DATA_SRC.exists():
        sys.path.insert(0, str(MARKET_DATA_SRC))
    from market_data.options_calculations import (
        black_scholes_price,
        calculate_option_greeks,
        estimate_risk_free_rate,
    )
except Exception:
    black_scholes_price = None
    calculate_option_greeks = None
    estimate_risk_free_rate = None

try:
    from market_data.env_settings import redis_config as _redis_env_config, resolve_instrument_symbol
except Exception:
    _redis_env_config = None
    resolve_instrument_symbol = None

try:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from redis_key_manager import get_redis_key
except Exception:
    def get_redis_key(key: str, *args, **kwargs):
        return key

# Redis configuration for virtual time
if _redis_env_config is not None:
    _r_cfg = _redis_env_config(decode_responses=True)
    REDIS_HOST = _r_cfg.get("host")
    REDIS_PORT = int(_r_cfg.get("port"))
else:
    REDIS_HOST = os.getenv("REDIS_HOST") or os.getenv("DEFAULT_REDIS_HOST") or "localhost"
    REDIS_PORT = int(os.getenv("REDIS_PORT") or os.getenv("DEFAULT_REDIS_PORT") or "6379")

_default_instrument_raw = (
    (resolve_instrument_symbol() if resolve_instrument_symbol else "")
    or os.getenv("INSTRUMENT_SYMBOL", "").strip()
    or os.getenv("INSTRUMENT_KEY", "").strip()
)
DEFAULT_INSTRUMENT = "" if _default_instrument_raw == "INSTRUMENT_NOT_SET" else _default_instrument_raw
_PLACEHOLDER_INSTRUMENTS = {"FALLBACK_TEST"}


def _is_placeholder_instrument(value: Any) -> bool:
    return str(value or "").strip().upper() in _PLACEHOLDER_INSTRUMENTS

def get_virtual_time_info():
    """Get virtual time status and current time."""
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
        enabled = r.get("system:virtual_time:enabled")
        current_time = r.get("system:virtual_time:current")
        
        if enabled and current_time:
            return {
                "enabled": True,
                "current_time": datetime.fromisoformat(current_time.decode('utf-8'))
            }
    except Exception as e:
        logger.warning(f"Could not get virtual time info: {e}")
    
    return {"enabled": False, "current_time": None}


def _parse_timestamp_flexible(value: Any) -> Optional[datetime]:
    """Parse various timestamp representations into a timezone-aware datetime (UTC)."""
    if value is None:
        return None

    # Numeric epoch support (seconds or milliseconds)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None

        # Numeric epoch string support
        if raw.isdigit():
            try:
                num = int(raw)
                if num > 1e12:
                    num = num / 1000
                return datetime.fromtimestamp(num, tz=timezone.utc)
            except Exception:
                return None

        normalized = raw
        # "YYYY-MM-DD HH:MM:SS" -> ISO-like
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T", 1)
        # +0530 -> +05:30 (strict parser compatibility)
        if len(normalized) >= 5 and (normalized[-5] in "+-") and normalized[-3] != ":":
            if normalized[-4:].isdigit():
                normalized = f"{normalized[:-5]}{normalized[-5:-2]}:{normalized[-2:]}"
        normalized = normalized.replace("Z", "+00:00")

        try:
            dt = datetime.fromisoformat(normalized)
        except Exception:
            return None

        if dt.tzinfo is None:
            # Default naive timestamps to IST to match existing market time assumptions
            dt = dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
        return dt.astimezone(timezone.utc)

    return None


def _normalize_timestamp_string(value: Any) -> Any:
    """Normalize a timestamp-like value to ISO-8601 UTC string when parseable."""
    dt = _parse_timestamp_flexible(value)
    if not dt:
        return value
    return dt.isoformat().replace("+00:00", "Z")


def _normalize_timestamp_fields(payload: Any) -> Any:
    """Recursively normalize common timestamp/date fields in dict/list payloads."""
    if isinstance(payload, list):
        return [_normalize_timestamp_fields(item) for item in payload]

    if isinstance(payload, dict):
        normalized: Dict[str, Any] = {}
        for key, value in payload.items():
            key_l = str(key).lower()
            if isinstance(value, (dict, list)):
                normalized[key] = _normalize_timestamp_fields(value)
            elif any(
                token in key_l
                for token in ["timestamp", "_at", "date", "time"]
            ):
                normalized[key] = _normalize_timestamp_string(value)
            else:
                normalized[key] = value
        return normalized

    return payload

def filter_data_by_virtual_time(data, time_field="start_at"):
    """Filter data to only include records up to current virtual time."""
    virtual_time_info = get_virtual_time_info()
    
    if not virtual_time_info["enabled"] or not virtual_time_info["current_time"]:
        return data
    
    current_virtual_time = virtual_time_info["current_time"]
    
    # Filter data based on timestamp
    filtered_data = []
    for item in data:
        item_time_str = item.get(time_field) or item.get("timestamp")
        if item_time_str:
            item_time = _parse_timestamp_flexible(item_time_str)
            if item_time is None:
                # If we can't parse the timestamp, include the item
                filtered_data.append(item)
                continue

            compare_time = current_virtual_time
            if compare_time.tzinfo is None:
                compare_time = compare_time.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
            compare_time = compare_time.astimezone(timezone.utc)

            if item_time <= compare_time:
                filtered_data.append(item)
    
    return filtered_data


def _redis_sync_client() -> redis.Redis:
    """Create a short-timeout Redis client for HTTP request handlers."""
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=0,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _timeframe_aliases(timeframe: str) -> List[str]:
    tf = (timeframe or "").strip()
    if not tf:
        return ["1min"]
    out = [tf]
    tfl = tf.lower()
    # Support both "5min" and legacy "5m" style keys.
    if tfl.endswith("min"):
        out.append(tfl.replace("min", "m"))
    elif tfl.endswith("m") and not tfl.endswith("min"):
        # Best effort "5m" -> "5min"
        digits = tfl[:-1]
        if digits.isdigit():
            out.append(f"{digits}min")
    return list(dict.fromkeys(out))


def _ohlc_sorted_keys_to_try(
    instrument: str,
    timeframe: str,
    preferred_mode: Optional[str] = None,
    strict_mode: bool = False,
) -> List[str]:
    tfs = _timeframe_aliases(timeframe)
    prefixes = ["live", "historical", "paper", ""]
    if preferred_mode in {"live", "historical", "paper"}:
        if strict_mode:
            prefixes = [preferred_mode]
        else:
            prefixes = [preferred_mode] + [p for p in prefixes if p != preferred_mode]
    keys: List[str] = []
    for tf in tfs:
        for p in prefixes:
            if p:
                keys.append(f"{p}:ohlc_sorted:{instrument}:{tf}")
            else:
                keys.append(f"ohlc_sorted:{instrument}:{tf}")
    return keys


def _extract_key_mode(redis_key: Optional[str]) -> Optional[str]:
    """Extract mode prefix from Redis key (live/historical/paper)."""
    if not redis_key:
        return None
    if redis_key.startswith("live:"):
        return "live"
    if redis_key.startswith("historical:"):
        return "historical"
    if redis_key.startswith("paper:"):
        return "paper"
    return None


def _parse_ohlc_json_rows(rows: List[str], timeframe: str = "1min") -> List[Dict[str, Any]]:
    bars: List[Dict[str, Any]] = []
    for row in rows:
        if not row:
            continue
        try:
            bars.append(json.loads(row))
        except Exception:
            continue

    return _merge_ohlc_bars_by_timeframe(bars, timeframe)


def _read_ohlc_from_redis(
    instrument: str,
    timeframe: str,
    limit: int = 100,
    order: str = "asc",
    preferred_mode: Optional[str] = None,
    strict_mode: bool = False,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Read OHLC bars from Redis sorted sets, trying multiple key patterns."""
    r = _redis_sync_client()

    keys = _ohlc_sorted_keys_to_try(
        instrument, timeframe, preferred_mode=preferred_mode, strict_mode=strict_mode
    )
    for key in keys:
        try:
            count = r.zcard(key)
            if not count:
                continue

            lim = max(int(limit or 0), 1)
            if (order or "asc").lower() == "desc":
                rows = r.zrevrange(key, 0, lim - 1)
                bars = _parse_ohlc_json_rows(rows, timeframe=timeframe)
                bars = list(reversed(bars))
                return bars, key

            # asc: return latest lim bars in ascending order
            start = -lim
            end = -1
            rows = r.zrange(key, start, end)
            bars = _parse_ohlc_json_rows(rows, timeframe=timeframe)
            return bars, key
        except Exception:
            continue

    return [], None


def _discover_instruments_from_redis(max_instruments: int = 25) -> List[str]:
    """Best-effort discovery of instruments present in Redis OHLC sorted-set keys.

    Looks for keys like:
      - live:ohlc_sorted:{instrument}:{timeframe}
      - historical:ohlc_sorted:{instrument}:{timeframe}
      - ohlc_sorted:{instrument}:{timeframe}
    """
    try:
        r = _redis_sync_client()
    except Exception:
        return []

    patterns = ["*:ohlc_sorted:*:*", "ohlc_sorted:*:*"]
    instruments: set[str] = set()
    for pat in patterns:
        cursor = 0
        while True:
            try:
                cursor, keys = r.scan(cursor=cursor, match=pat, count=500)
            except Exception:
                break

            for key in keys or []:
                try:
                    parts = str(key).split(":")
                    inst: Optional[str] = None
                    # live:ohlc_sorted:INST:TF
                    if len(parts) >= 4 and parts[1] == "ohlc_sorted":
                        inst = parts[2]
                    # ohlc_sorted:INST:TF
                    elif len(parts) >= 3 and parts[0] == "ohlc_sorted":
                        inst = parts[1]
                    if inst:
                        if _is_placeholder_instrument(inst):
                            continue
                        instruments.add(inst)
                        if len(instruments) >= int(max_instruments or 0):
                            return sorted(instruments)
                except Exception:
                    continue

            if cursor == 0:
                break

    return sorted(instruments)


def _select_most_active_instrument(
    instruments: List[str],
    preferred_mode: str = "live",
) -> Optional[str]:
    """Pick the instrument with the highest OHLC sorted-set activity."""
    cleaned: List[str] = []
    seen: set[str] = set()
    for inst in instruments or []:
        val = str(inst or "").strip().upper()
        if not val or val in seen or _is_placeholder_instrument(val):
            continue
        seen.add(val)
        cleaned.append(val)
    if not cleaned:
        return None

    try:
        r = _redis_sync_client()
    except Exception:
        return cleaned[0]

    mode_pref = str(preferred_mode or "").strip().lower()
    mode_order_raw = [mode_pref, "live", "historical", "paper", ""]
    mode_order: List[str] = []
    for m in mode_order_raw:
        if m not in mode_order:
            mode_order.append(m)

    timeframe_aliases = ("1m", "5m", "15m", "1min", "5min", "15min")
    best_inst: Optional[str] = None
    best_score = -1
    for inst in cleaned:
        score = 0
        for m in mode_order:
            for tf in timeframe_aliases:
                key = f"{m}:ohlc_sorted:{inst}:{tf}" if m else f"ohlc_sorted:{inst}:{tf}"
                try:
                    score += int(r.zcard(key) or 0)
                except Exception:
                    continue
        if score > best_score:
            best_score = score
            best_inst = inst

    if best_inst and best_score > 0:
        return best_inst
    return cleaned[0]


def _extract_bar_timestamp(bar: Dict[str, Any]) -> Optional[str]:
    return bar.get("start_at") or bar.get("timestamp")


def _merge_ohlc_bars_by_timeframe(data: List[Dict[str, Any]], timeframe: str) -> List[Dict[str, Any]]:
    """Merge OHLC snapshots into one canonical bar per timeframe bucket."""
    if not data:
        return []

    tf = str(timeframe or "1min")

    def _num(v: Any) -> Optional[float]:
        try:
            if v is None or v == "":
                return None
            return float(v)
        except Exception:
            return None

    def _volume(v: Any) -> float:
        try:
            if v is None or v == "":
                return 0.0
            return float(v)
        except Exception:
            return 0.0

    def _parse_dt(raw_ts: Any) -> Optional[datetime]:
        if raw_ts is None:
            return None
        if isinstance(raw_ts, datetime):
            return raw_ts
        if isinstance(raw_ts, (int, float)):
            try:
                ts = float(raw_ts)
                if ts > 1e12:
                    ts = ts / 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                return None
        if isinstance(raw_ts, str):
            s = raw_ts.strip()
            if not s:
                return None
            if " " in s and "T" not in s:
                s = s.replace(" ", "T", 1)
            s = s.replace("Z", "+00:00")
            if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":" and s[-4:].isdigit():
                s = f"{s[:-5]}{s[-5:-2]}:{s[-2:]}"
            try:
                dt = datetime.fromisoformat(s)
            except Exception:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST_TZ)
            return dt
        return None

    buckets: Dict[str, Dict[str, Any]] = {}
    first_seen_order: List[str] = []

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue

        ts_raw = _extract_bar_timestamp(item)
        ts = _parse_dt(ts_raw)
        if ts is None:
            synthetic_key = f"__idx_{idx}"
            buckets[synthetic_key] = dict(item)
            first_seen_order.append(synthetic_key)
            continue

        bucket_dt = _bucket_start(ts, tf)
        bucket_key = bucket_dt.isoformat()

        if bucket_key not in buckets:
            row = dict(item)
            row["start_at"] = bucket_key
            buckets[bucket_key] = row
            first_seen_order.append(bucket_key)
            continue

        existing = buckets[bucket_key]

        e_high = _num(existing.get("high"))
        n_high = _num(item.get("high"))
        if e_high is not None and n_high is not None:
            existing["high"] = max(e_high, n_high)
        elif n_high is not None:
            existing["high"] = n_high

        e_low = _num(existing.get("low"))
        n_low = _num(item.get("low"))
        if e_low is not None and n_low is not None:
            existing["low"] = min(e_low, n_low)
        elif n_low is not None:
            existing["low"] = n_low

        # Prefer close/oi from the newest snapshot proxy (higher cumulative volume).
        e_vol = _volume(existing.get("volume"))
        n_vol = _volume(item.get("volume"))
        if n_vol >= e_vol:
            if "close" in item:
                existing["close"] = item.get("close")
            existing["volume"] = item.get("volume")
            if "oi" in item:
                existing["oi"] = item.get("oi")
            if "open_interest" in item:
                existing["open_interest"] = item.get("open_interest")
            if "timestamp" in item:
                existing["timestamp"] = item.get("timestamp")

        existing["start_at"] = bucket_key

    # Always return ascending by bucket timestamp for chart stability.
    parse_cache: Dict[str, datetime] = {}

    def _sort_key(k: str) -> datetime:
        if k in parse_cache:
            return parse_cache[k]
        if k.startswith("__idx_"):
            parse_cache[k] = datetime.min.replace(tzinfo=timezone.utc)
            return parse_cache[k]
        try:
            dt = datetime.fromisoformat(k.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.min.replace(tzinfo=timezone.utc)
        parse_cache[k] = dt
        return dt

    ordered_keys = sorted(first_seen_order, key=_sort_key)
    return [buckets[k] for k in ordered_keys if k in buckets]


def _determine_base_limit(timeframe: str, requested_limit: int) -> int:
    """Choose how many 1-min bars to request when aggregating higher timeframes."""
    tf = timeframe.lower()
    # Rough multiplier based on bar size
    if tf.endswith("min"):
        try:
            minutes = int(tf.replace("min", ""))
        except ValueError:
            minutes = 1
        multiplier = max(minutes, 1)
    elif tf.endswith("h"):
        try:
            hours = int(tf.replace("h", ""))
        except ValueError:
            hours = 1
        multiplier = max(hours * 60, 60)
    elif tf.endswith("d"):
        multiplier = 1440  # full trading day worth of minutes
    else:
        multiplier = 1

    # Ensure we request enough data but cap to avoid abuse
    base_limit = requested_limit if requested_limit and requested_limit > 0 else 100
    return min(max(base_limit * multiplier, base_limit, 300), 2000)


def _bucket_start(ts: datetime, timeframe: str) -> datetime:
    """Floor a timestamp to the start of the requested bucket."""
    tf = timeframe.lower()
    if tf.endswith("min") or (tf.endswith("m") and tf[:-1].isdigit()):
        try:
            minutes = int(tf.replace("min", "").replace("m", ""))
        except ValueError:
            minutes = 1
        minute_bucket = (ts.minute // minutes) * minutes
        return ts.replace(minute=minute_bucket, second=0, microsecond=0)
    if tf.endswith("h"):
        try:
            hours = int(tf.replace("h", ""))
        except ValueError:
            hours = 1
        hour_bucket = (ts.hour // hours) * hours
        return ts.replace(hour=hour_bucket, minute=0, second=0, microsecond=0)
    if tf.endswith("d"):
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return ts.replace(second=0, microsecond=0)


def aggregate_ohlc(data: List[Dict[str, Any]], timeframe: str) -> List[Dict[str, Any]]:
    """Aggregate 1-minute OHLC bars into a higher timeframe."""
    if timeframe == "1min":
        return data

    def _num(v: Any, default: float = 0.0) -> float:
        try:
            if v is None:
                return default
            return float(v)
        except Exception:
            return default

    def _oi(v: Any) -> Optional[float]:
        try:
            if v is None:
                return None
            return float(v)
        except Exception:
            return None

    buckets: Dict[str, Dict[str, Any]] = {}

    for item in data:
        ts_str: Optional[str] = item.get("start_at") or item.get("timestamp")
        if not ts_str:
            continue

        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue

        bucket_start = _bucket_start(ts, timeframe)
        bucket_key = bucket_start.isoformat()

        if bucket_key not in buckets:
            latest_oi = _oi(item.get("oi") if item.get("oi") is not None else item.get("open_interest"))
            buckets[bucket_key] = {
                "instrument": item.get("instrument"),
                "timeframe": timeframe,
                "open": _num(item.get("open", item.get("last_price", 0))),
                "high": _num(item.get("high", item.get("last_price", 0))),
                "low": _num(item.get("low", item.get("last_price", 0))),
                "close": _num(item.get("close", item.get("last_price", 0))),
                "volume": _num(item.get("volume"), 0.0),
                "oi": latest_oi,
                "start_at": bucket_key
            }
        else:
            bucket = buckets[bucket_key]
            bucket["high"] = max(bucket["high"], _num(item.get("high", bucket["high"]), bucket["high"]))
            bucket["low"] = min(bucket["low"], _num(item.get("low", bucket["low"]), bucket["low"]))
            bucket["close"] = _num(item.get("close", bucket["close"]), bucket["close"])
            bucket["volume"] = _num(bucket.get("volume"), 0.0) + _num(item.get("volume"), 0.0)

            next_oi = _oi(item.get("oi") if item.get("oi") is not None else item.get("open_interest"))
            if next_oi is not None:
                bucket["oi"] = next_oi

    # Return buckets sorted by time
    ordered = [buckets[k] for k in sorted(buckets.keys())]
    return ordered


def _has_any_oi(data: List[Dict[str, Any]]) -> bool:
    """Return True when at least one bar carries OI/open_interest."""
    for item in data or []:
        v = item.get("oi")
        if v is None:
            v = item.get("open_interest")
        if v is not None and str(v) != "":
            return True
    return False


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _format_chart_labels(timestamps: List[Any], timeframe: str) -> List[str]:
    """Pre-format chart labels server-side for thin frontend rendering."""
    if not timestamps:
        return []

    market_tz = timezone(timedelta(hours=5, minutes=30))
    parsed_local: List[Optional[datetime]] = []
    valid_ms: List[float] = []

    for value in timestamps:
        dt = _parse_timestamp_flexible(value)
        if not dt:
            parsed_local.append(None)
            continue

        local_dt = dt.astimezone(market_tz)
        parsed_local.append(local_dt)
        valid_ms.append(local_dt.timestamp() * 1000.0)

    if not valid_ms:
        return ["Invalid Date" for _ in timestamps]

    span_ms = max(valid_ms) - min(valid_ms)
    tf = str(timeframe or "").strip().lower()
    include_date = tf == "1d" or span_ms >= 24 * 60 * 60 * 1000
    fmt = "%m-%d %H:%M" if include_date else "%H:%M"

    return [dt.strftime(fmt) if dt else "Invalid Date" for dt in parsed_local]


def _calculate_rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    if not closes:
        return []
    if period <= 0 or len(closes) < period + 1:
        return [None] * len(closes)

    rsi: List[Optional[float]] = []
    gains = 0.0
    losses = 0.0

    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change

    avg_gain = gains / period
    avg_loss = losses / period

    rsi.extend([None] * period)

    for i in range(period, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        rs = 100.0 if avg_loss == 0 else (avg_gain / avg_loss)
        rsi.append(100.0 - (100.0 / (1.0 + rs)))

    return rsi


def _calculate_ema_series(values: List[float], period: int) -> List[Optional[float]]:
    if not values:
        return []
    if period <= 0:
        return [None] * len(values)

    ema: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return ema

    sma = sum(values[:period]) / period
    ema[period - 1] = sma
    multiplier = 2.0 / (period + 1)

    for i in range(period, len(values)):
        prev = ema[i - 1]
        if prev is None:
            prev = values[i - 1]
        ema[i] = (values[i] - prev) * multiplier + prev

    return ema


def _calculate_macd_series(
    closes: List[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    if not closes:
        return [], []

    ema_fast = _calculate_ema_series(closes, fast_period)
    ema_slow = _calculate_ema_series(closes, slow_period)

    macd: List[Optional[float]] = []
    for i in range(len(closes)):
        f = ema_fast[i] if i < len(ema_fast) else None
        s = ema_slow[i] if i < len(ema_slow) else None
        if f is None or s is None:
            macd.append(None)
        else:
            macd.append(float(f - s))

    non_null_macd = [v for v in macd if v is not None]
    signal_raw = _calculate_ema_series(non_null_macd, signal_period)
    aligned_signal: List[Optional[float]] = [None] * len(macd)

    first_macd_idx = next((i for i, v in enumerate(macd) if v is not None), -1)
    if first_macd_idx >= 0:
        for j, val in enumerate(signal_raw):
            if val is None:
                continue
            idx = first_macd_idx + j
            if idx < len(aligned_signal):
                aligned_signal[idx] = val

    return macd, aligned_signal


def _build_chart_payload_from_ohlc(
    instrument: str,
    timeframe: str,
    ohlc_data: List[Dict[str, Any]],
    req_limit: int,
    indicators_bars_needed: int = 120,
) -> Dict[str, Any]:
    if not ohlc_data:
        return {
            "instrument": instrument,
            "timeframe": timeframe,
            "price_chart": {
                "timestamps": [],
                "labels": [],
                "prices": [],
                "volumes_millions": [],
                "oi": [],
                "volume_label": "Volume",
                "volume_axis_title": "Volume",
            },
            "indicators_chart": {
                "timestamps": [],
                "labels": [],
                "rsi": [],
                "macd": [],
                "signal": [],
                "rsi_period": 14,
                "macd_label": "MACD",
                "macd_signal_label": "MACD Signal",
                "has_macd": False,
            },
        }

    price_data = ohlc_data[-req_limit:]
    price_timestamps = [_extract_bar_timestamp(d) for d in price_data]
    price_labels = _format_chart_labels(price_timestamps, timeframe)
    prices = [_safe_float(d.get("close"), 0.0) or 0.0 for d in price_data]
    volumes_raw = [_safe_float(d.get("volume"), 0.0) or 0.0 for d in price_data]
    max_volume = max(volumes_raw) if volumes_raw else 0.0
    if max_volume >= 1_000_000.0:
        volume_scale = 1_000_000.0
        volume_suffix = "M"
    elif max_volume >= 1_000.0:
        volume_scale = 1_000.0
        volume_suffix = "K"
    else:
        volume_scale = 1.0
        volume_suffix = ""
    volumes_scaled = [v / volume_scale for v in volumes_raw]
    volume_label = f"Volume ({volume_suffix})" if volume_suffix else "Volume"
    oi_series = []
    for d in price_data:
        oi_val = d.get("oi") if d.get("oi") is not None else d.get("open_interest")
        oi_series.append(_safe_float(oi_val, None))

    ohlc_for_indicators = ohlc_data[-indicators_bars_needed:]
    closes = [_safe_float(d.get("close"), 0.0) or 0.0 for d in ohlc_for_indicators]
    indicator_timestamps_full = [_extract_bar_timestamp(d) for d in ohlc_for_indicators]

    rsi_period = 14 if len(closes) >= 15 else max(3, len(closes) - 1)
    rsi_values = _calculate_rsi_series(closes, rsi_period)

    if len(closes) >= 26:
        fast, slow, signal = 12, 26, 9
        adaptive = False
    elif len(closes) >= 4:
        fast = max(2, round(len(closes) * 0.35))
        slow = max(3, round(len(closes) * 0.7))
        signal = max(2, round(len(closes) * 0.2))
        adaptive = True
    else:
        fast = slow = signal = 0
        adaptive = False

    if len(closes) >= 4:
        macd_values, signal_values = _calculate_macd_series(closes, fast, slow, signal)
        macd_label = f"MACD ({fast}/{slow}, warm-up)" if adaptive else "MACD"
        macd_signal_label = f"Signal ({signal}, warm-up)" if adaptive else "MACD Signal"
    else:
        macd_values = [None] * len(closes)
        signal_values = [None] * len(closes)
        macd_label = "MACD (warming up)"
        macd_signal_label = "MACD Signal (warming up)"

    chart_points = min(50, len(ohlc_for_indicators))
    start_idx = max(0, len(ohlc_for_indicators) - chart_points)

    indicator_timestamps = indicator_timestamps_full[start_idx:]
    indicator_labels = _format_chart_labels(indicator_timestamps, timeframe)
    chart_rsi = rsi_values[start_idx:]
    chart_macd = macd_values[start_idx:]
    chart_signal = signal_values[start_idx:]
    has_macd = any(v is not None for v in chart_macd)

    return {
        "instrument": instrument,
        "timeframe": timeframe,
        "price_chart": {
            "timestamps": price_timestamps,
            "labels": price_labels,
            "prices": prices,
            "volumes_millions": volumes_scaled,
            "oi": oi_series,
            "volume_label": volume_label,
            "volume_axis_title": volume_label,
        },
        "indicators_chart": {
            "timestamps": indicator_timestamps,
            "labels": indicator_labels,
            "rsi": chart_rsi,
            "macd": chart_macd,
            "signal": chart_signal,
            "rsi_period": rsi_period,
            "macd_label": macd_label,
            "macd_signal_label": macd_signal_label,
            "has_macd": has_macd,
        },
    }

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Market Data Dashboard",
    description="Standalone market data monitoring and visualization",
    version="1.0.0"
)

# Mount static files (optional - create directory if needed)
import os
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Setup templates
templates = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates))

# Market Data API configuration
MARKET_DATA_API_URL = os.getenv("MARKET_DATA_API_URL") or (
    f"http://{os.getenv('MARKET_DATA_API_HOST', 'localhost')}:"
    f"{os.getenv('MARKET_DATA_API_PORT', '8004')}"
)

# Lightweight in-memory caches to keep UI responsive when upstream API is slow.
_LAST_GOOD_INDICATORS: Dict[str, Dict[str, Any]] = {}
_LAST_GOOD_DEPTH: Dict[str, Dict[str, Any]] = {}
_LAST_GOOD_OPTIONS: Dict[str, Dict[str, Any]] = {}

PUBLIC_SCHEMA_VERSION = "v1"
PUBLIC_TOPICS: Tuple[str, ...] = ("mode", "tick", "ohlc", "indicators", "depth", "options")
PUBLIC_TIMEFRAMES: Tuple[str, ...] = ("1m", "5m", "15m")
PUBLIC_TIMEFRAME_ALIASES: Dict[str, List[str]] = {
    "1m": ["1m", "1min", "minute"],
    "5m": ["5m", "5min"],
    "15m": ["15m", "15min"],
}

# Trading terminal runtime state (paper runner process managed by dashboard UI).
REPO_ROOT = Path(__file__).parent.parent
ML_PIPELINE_SRC = REPO_ROOT / "ml_pipeline" / "src"
DEFAULT_TRADING_EVENTS_PATH = REPO_ROOT / "ml_pipeline" / "artifacts" / "t33_paper_capital_events_actual.jsonl"
DEFAULT_TRADING_STDOUT_PATH = REPO_ROOT / "ml_pipeline" / "artifacts" / "t33_paper_capital_runner_stdout.log"
DEFAULT_TRADING_STDERR_PATH = REPO_ROOT / "ml_pipeline" / "artifacts" / "t33_paper_capital_runner_stderr.log"
DEFAULT_MODEL_EVAL_SUMMARY_PATH = (
    REPO_ROOT
    / "ml_pipeline"
    / "artifacts"
    / "models"
    / "by_features"
    / "core_v2"
    / "h5_ts0_lgbm_regime"
    / "reports"
    / "evaluation"
    / "openfe_v9_dual_eval_summary.json"
)
DEFAULT_MODEL_TRAINING_REPORT_PATH = (
    REPO_ROOT
    / "ml_pipeline"
    / "artifacts"
    / "models"
    / "by_features"
    / "core_v2"
    / "h5_ts0_lgbm_regime"
    / "reports"
    / "training"
    / "openfe_v9_dual_modeling_report.json"
)
DEFAULT_MODEL_POLICY_REPORT_PATH = (
    REPO_ROOT
    / "ml_pipeline"
    / "artifacts"
    / "models"
    / "by_features"
    / "core_v2"
    / "h5_ts0_lgbm_regime"
    / "config"
    / "profiles"
    / "openfe_v9_dual"
    / "threshold_report.json"
)
TRADING_MODEL_CATALOG_DIR = REPO_ROOT / "ml_pipeline" / "model_catalog" / "models"

_TRADING_LOCK = threading.Lock()
_TRADING_DEFAULT_INSTANCE = "default"
_TRADING_RUNNERS: Dict[str, Dict[str, Any]] = {}
_TRADING_LAST_BACKTEST: Dict[str, Dict[str, Any]] = {}
_TRADING_BACKTEST_STATE_DIR = REPO_ROOT / "ml_pipeline" / "artifacts" / "dashboard_state"


def _safe_load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
        return None
    except Exception:
        return None


def _extract_training_coverage_range(payload: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], int]:
    """Best-effort extraction of training date coverage from training cycle report."""
    if not isinstance(payload, dict):
        return None, None, 0

    best = payload.get("best_experiment")
    if not isinstance(best, dict):
        return None, None, 0
    result = best.get("result")
    if not isinstance(result, dict):
        return None, None, 0

    day_values: set[str] = set()
    for side in ("ce", "pe"):
        side_payload = result.get(side)
        if not isinstance(side_payload, dict):
            continue
        folds = side_payload.get("folds")
        if not isinstance(folds, list):
            continue
        for fold in folds:
            if not isinstance(fold, dict):
                continue
            days_payload = fold.get("days")
            if not isinstance(days_payload, dict):
                continue
            for key in ("train_days", "valid_days", "test_days"):
                values = days_payload.get(key)
                if not isinstance(values, list):
                    continue
                for raw in values:
                    text = str(raw or "").strip()
                    if len(text) < 10:
                        continue
                    day_text = text[:10]
                    try:
                        # Validate ISO day-like value.
                        datetime.fromisoformat(day_text)
                        day_values.add(day_text)
                    except Exception:
                        continue

    if not day_values:
        return None, None, 0
    ordered = sorted(day_values)
    return ordered[0], ordered[-1], len(ordered)


def _extract_dual_policy_eval(policy_payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of dual policy evaluation block."""
    if not isinstance(policy_payload, dict):
        return None
    dual = policy_payload.get("dual_mode_policy")
    if not isinstance(dual, dict):
        return None
    test_eval = dual.get("test_eval")
    if isinstance(test_eval, dict):
        return test_eval
    holdout_eval = dual.get("holdout_eval")
    if isinstance(holdout_eval, dict):
        return holdout_eval
    return None


def _summary_from_policy_eval(policy_payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Map policy eval schema into summary-like fields used by terminal UI."""
    dual_eval = _extract_dual_policy_eval(policy_payload)
    if isinstance(dual_eval, dict):
        trades = dual_eval.get("trades_total", dual_eval.get("trades"))
        trades = int(trades) if trades is not None else None
        avg_trades_day = dual_eval.get("trades_per_day", dual_eval.get("avg_trades_per_day"))
        if avg_trades_day is None:
            trade_rate = dual_eval.get("trade_rate")
            rows_total = dual_eval.get("rows_total", dual_eval.get("rows"))
            if trade_rate is not None and rows_total is not None:
                try:
                    avg_trades_day = float(trade_rate) * float(rows_total)
                except Exception:
                    avg_trades_day = None
        return {
            "days": None,
            "trades": trades,
            "avg_trades_per_day": avg_trades_day,
            "win_rate": dual_eval.get("win_rate"),
            "mean_net_return_per_trade": dual_eval.get("mean_net_return_per_trade", dual_eval.get("mean_net_per_trade")),
            "net_return_sum": dual_eval.get("net_return_sum", dual_eval.get("total_net_return")),
        }

    # Training-cycle utility report fallback.
    if isinstance(policy_payload, dict):
        best = policy_payload.get("best_experiment")
        if isinstance(best, dict):
            result = best.get("result")
            if isinstance(result, dict):
                utility = result.get("trading_utility")
                if isinstance(utility, dict):
                    trades = utility.get("trades_total")
                    try:
                        trades = int(trades) if trades is not None else None
                    except Exception:
                        trades = None
                    return {
                        "days": None,
                        "trades": trades,
                        "avg_trades_per_day": None,
                        "win_rate": utility.get("win_rate"),
                        "mean_net_return_per_trade": utility.get("mean_net_return_per_trade"),
                        "net_return_sum": utility.get("net_return_sum"),
                    }
    return None


def _resolve_repo_path(raw: Optional[str], default_path: Optional[Path] = None) -> Optional[Path]:
    text = str(raw or "").strip()
    if not text:
        return default_path
    p = Path(text)
    if p.is_absolute():
        return p
    return REPO_ROOT / p


def _path_text(path: Optional[Path]) -> str:
    if not isinstance(path, Path):
        return ""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except Exception:
        return str(path)


def _first_existing_path(candidates: Sequence[Path]) -> Optional[Path]:
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return None


def _build_model_eval_snapshot(
    summary_file: Optional[Path],
    training_file: Optional[Path],
    policy_file: Optional[Path],
) -> Dict[str, Any]:
    summary_payload = _safe_load_json(summary_file) if isinstance(summary_file, Path) else None
    training_payload = _safe_load_json(training_file) if isinstance(training_file, Path) else None
    policy_payload = _safe_load_json(policy_file) if isinstance(policy_file, Path) else None

    full_oos = summary_payload.get("full_oos") if isinstance(summary_payload, dict) else None
    latest_oos = summary_payload.get("latest_oos_slice") if isinstance(summary_payload, dict) else None
    coverage_start, coverage_end, coverage_days = _extract_training_coverage_range(training_payload)
    if full_oos is None:
        full_oos = _summary_from_policy_eval(policy_payload)

    training_best_id = None
    training_best_obj = None
    if isinstance(training_payload, dict):
        best_experiment = training_payload.get("best_experiment")
        if isinstance(best_experiment, dict):
            training_best_id = best_experiment.get("experiment_id")
            training_best_obj = best_experiment.get("objective_value")

    policy_mode = None
    policy_topk = None
    policy_ce_threshold = None
    policy_pe_threshold = None
    policy_label_target = None
    if isinstance(policy_payload, dict):
        dual_policy = policy_payload.get("dual_mode_policy")
        if isinstance(dual_policy, dict):
            policy_mode = dual_policy.get("selection_mode")
            policy_topk = dual_policy.get("topk_per_day")
            policy_ce_threshold = dual_policy.get("ce_threshold")
            policy_pe_threshold = dual_policy.get("pe_threshold")
        else:
            # Training-cycle utility config fallback (thresholds embedded here).
            utility_cfg = policy_payload.get("trading_utility_config")
            if isinstance(utility_cfg, dict):
                policy_mode = "threshold"
                policy_ce_threshold = utility_cfg.get("ce_threshold")
                policy_pe_threshold = utility_cfg.get("pe_threshold")
            else:
                # Modeling-v2 threshold report fallback.
                raw_ce = policy_payload.get("ce_threshold")
                raw_pe = policy_payload.get("pe_threshold")
                if raw_ce is not None or raw_pe is not None:
                    policy_mode = "threshold"
                    policy_ce_threshold = raw_ce
                    policy_pe_threshold = raw_pe
        policy_label_target = policy_payload.get("label_target")

    if policy_payload is None:
        runner_supports_policy = False
        compatibility_note = "No policy report found. Provide a threshold report before running this model."
    else:
        runner_supports_policy = (str(policy_mode or "threshold").lower() == "threshold")
        compatibility_note = (
            "Runner currently supports threshold policy directly."
            if runner_supports_policy
            else "Policy is topk-based; keep this as evaluation reference unless runner is updated for topk."
        )

    return {
        "files": {
            "summary_path": _path_text(summary_file),
            "training_report_path": _path_text(training_file),
            "policy_report_path": _path_text(policy_file),
            "summary_exists": bool(summary_payload is not None),
            "training_exists": bool(training_payload is not None),
            "policy_exists": bool(policy_payload is not None),
        },
        "model_quality": {
            "full_oos": full_oos,
            "latest_oos_slice": latest_oos,
            "config": summary_payload.get("config") if isinstance(summary_payload, dict) else None,
        },
        "training": {
            "objective": training_payload.get("objective") if isinstance(training_payload, dict) else None,
            "label_target": training_payload.get("label_target") if isinstance(training_payload, dict) else None,
            "best_experiment_id": training_best_id,
            "best_objective_value": training_best_obj,
            "experiments_total": training_payload.get("experiments_total") if isinstance(training_payload, dict) else None,
            "rows_total": training_payload.get("rows_total") if isinstance(training_payload, dict) else None,
            "days_total": training_payload.get("days_total") if isinstance(training_payload, dict) else None,
            "coverage_start_date": coverage_start,
            "coverage_end_date": coverage_end,
            "coverage_days_count": coverage_days,
        },
        "policy": {
            "selection_mode": policy_mode,
            "topk_per_day": policy_topk,
            "ce_threshold": policy_ce_threshold,
            "pe_threshold": policy_pe_threshold,
            "label_target": policy_label_target,
        },
        "runner_compatibility": {
            "supports_policy_directly": runner_supports_policy,
            "note": compatibility_note,
        },
    }


def _build_catalog_entry(raw: Dict[str, Any], source: str = "curated", load_eval_snapshot: bool = True) -> Dict[str, Any]:
    instance_key = _normalize_trading_instance(raw.get("instance_key") or raw.get("profile_key") or "model")
    model_path = _resolve_repo_path(raw.get("model_package"))
    threshold_path = _resolve_repo_path(raw.get("threshold_report"))
    summary_path = _resolve_repo_path(raw.get("eval_summary_path"))
    training_path = _resolve_repo_path(raw.get("training_report_path"))

    if load_eval_snapshot:
        eval_snapshot = _build_model_eval_snapshot(summary_path, training_path, threshold_path)
    else:
        eval_snapshot = {
            "model_quality": {},
            "policy": {},
            "training": {},
            "runner_compatibility": {
                "supports_policy_directly": None,
                "note": "Evaluation snapshot is loaded on-demand from the model view endpoint.",
            },
        }
    full_oos = eval_snapshot.get("model_quality", {}).get("full_oos") if isinstance(eval_snapshot, dict) else {}
    latest_oos = eval_snapshot.get("model_quality", {}).get("latest_oos_slice") if isinstance(eval_snapshot, dict) else {}
    policy = eval_snapshot.get("policy", {}) if isinstance(eval_snapshot, dict) else {}
    training = eval_snapshot.get("training", {}) if isinstance(eval_snapshot, dict) else {}

    existence = {
        "model_package": bool(model_path and model_path.exists()),
        "threshold_report": bool(threshold_path and threshold_path.exists()),
        "eval_summary_path": bool(summary_path and summary_path.exists()),
        "training_report_path": bool(training_path and training_path.exists()),
    }
    missing_required: List[str] = []
    if not existence["model_package"]:
        missing_required.append("model_package")
    if not existence["threshold_report"]:
        missing_required.append("threshold_report")

    query_values = {"model": instance_key}
    if model_path:
        query_values["model_package"] = _path_text(model_path)
    if threshold_path:
        query_values["threshold_report"] = _path_text(threshold_path)
    if summary_path:
        query_values["eval_summary_path"] = _path_text(summary_path)
    if training_path:
        query_values["training_report_path"] = _path_text(training_path)
    prefill_url = f"/trading?{urlencode(query_values)}"

    eval_query: Dict[str, str] = {}
    if summary_path:
        eval_query["summary_path"] = _path_text(summary_path)
    if training_path:
        eval_query["training_report_path"] = _path_text(training_path)
    if threshold_path:
        eval_query["policy_report_path"] = _path_text(threshold_path)
    evaluation_api_url = f"/api/trading/model-evaluation?{urlencode(eval_query)}" if eval_query else ""

    return {
        "source": source,
        "instance_key": instance_key,
        "profile_key": str(raw.get("profile_key") or ""),
        "title": str(raw.get("title") or instance_key),
        "summary": str(raw.get("summary") or ""),
        "description": str(raw.get("description") or ""),
        "recommended": bool(raw.get("recommended")),
        "model_package": _path_text(model_path),
        "threshold_report": _path_text(threshold_path),
        "eval_summary_path": _path_text(summary_path),
        "training_report_path": _path_text(training_path),
        "exists": existence,
        "ready_to_run": len(missing_required) == 0,
        "missing_required": missing_required,
        "metrics": {
            "training_coverage_start": training.get("coverage_start_date"),
            "training_coverage_end": training.get("coverage_end_date"),
            "training_coverage_days": training.get("coverage_days_count"),
            "training_objective": training.get("objective"),
            "best_experiment_id": training.get("best_experiment_id"),
            "best_objective_value": training.get("best_objective_value"),
            "policy_mode": policy.get("selection_mode"),
            "ce_threshold": policy.get("ce_threshold"),
            "pe_threshold": policy.get("pe_threshold"),
            "full_oos_win_rate": (full_oos or {}).get("win_rate") if isinstance(full_oos, dict) else None,
            "full_oos_trades_per_day": (full_oos or {}).get("avg_trades_per_day") if isinstance(full_oos, dict) else None,
            "full_oos_net_per_trade": (full_oos or {}).get("mean_net_return_per_trade") if isinstance(full_oos, dict) else None,
            "full_oos_net_sum": (full_oos or {}).get("net_return_sum") if isinstance(full_oos, dict) else None,
            "latest_oos_win_rate": (latest_oos or {}).get("win_rate") if isinstance(latest_oos, dict) else None,
            "latest_oos_days": (latest_oos or {}).get("days") if isinstance(latest_oos, dict) else None,
        },
        "compatibility_note": eval_snapshot.get("runner_compatibility", {}).get("note"),
        "launch_url": f"/trading/model/{instance_key}",
        "prefill_url": prefill_url,
        "evaluation_api_url": evaluation_api_url,
    }


def _build_trading_model_catalog() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []

    manifest_paths = sorted(TRADING_MODEL_CATALOG_DIR.glob("*/model.json")) if TRADING_MODEL_CATALOG_DIR.exists() else []
    for manifest in manifest_paths:
        payload = _safe_load_json(manifest)
        if not isinstance(payload, dict):
            continue
        required = ("instance_key", "model_package", "threshold_report")
        if any(not str(payload.get(k) or "").strip() for k in required):
            continue
        entries.append(_build_catalog_entry(payload, source="catalog_manifest", load_eval_snapshot=True))

    return entries


def _get_current_mode_hint(timeout_seconds: float = 1.5) -> Optional[str]:
    """Best-effort mode lookup from upstream API (live/historical/paper)."""
    try:
        response = requests.get(
            f"{MARKET_DATA_API_URL}/api/v1/system/mode",
            timeout=timeout_seconds,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        mode = str(payload.get("mode") or "").strip().lower()
        if mode in {"live", "historical", "paper"}:
            return mode
    except Exception:
        return None
    return None


def _allow_synthetic_fallback(mode_hint: Optional[str]) -> bool:
    """Disable synthetic market data in live mode when strict mode is enabled."""
    strict_live_real_only = os.getenv("LIVE_STRICT_REAL_ONLY", "1").strip().lower() in {"1", "true", "yes", "on"}
    mode = str(mode_hint or _get_current_mode_hint() or "").strip().lower()
    if strict_live_real_only and mode == "live":
        return False
    return True


IST_TZ = timezone(timedelta(hours=5, minutes=30))


def _reference_price_for_options(instrument: str) -> Optional[float]:
    """Fetch latest price from Redis for synthetic options chain."""
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        keys = [
            f"ltp:{instrument}",
            f"live:ltp:{instrument}",
            get_redis_key(f"price:{instrument}:latest"),
            get_redis_key(f"price:{instrument.upper()}:latest"),
            f"price:{instrument}:latest",
        ]
        for key in keys:
            if not key:
                continue
            raw = r.get(key)
            if raw is None:
                continue
            try:
                # Structured JSON payload from LTP cache
                if isinstance(raw, str) and raw.startswith("{"):
                    obj = json.loads(raw)
                    cand = obj.get("last_price") or obj.get("close") or obj.get("price")
                    if cand:
                        return float(cand)
                # Fallback numeric
                cand = float(raw)
                return cand
            except Exception:
                continue
    except Exception:
        return None
    return None


def _current_time_for_mode(mode_hint: Optional[str] = None) -> datetime:
    """Return effective time (virtual when available)."""
    try:
        vt = get_virtual_time_info()
        if vt.get("enabled") and vt.get("current_time"):
            ct = vt["current_time"]
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=IST_TZ)
            return ct.astimezone(timezone.utc)
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    if mode_hint == "historical":
        return now
    return now


def _next_weekly_expiry_from(now_dt: datetime) -> datetime:
    """Next Thursday 15:30 IST from given time."""
    ist_now = now_dt.astimezone(IST_TZ)
    days_ahead = (3 - ist_now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    expiry = ist_now + timedelta(days=days_ahead)
    expiry = expiry.replace(hour=15, minute=30, second=0, microsecond=0)
    return expiry.astimezone(timezone.utc)


def _build_synthetic_options_chain_black_scholes(
    instrument: str,
    mode_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Generate a synthetic options chain using Black-Scholes for historical/offline mode."""
    if not black_scholes_price:
        return None

    spot = _reference_price_for_options(instrument) or 50000.0
    now_dt = _current_time_for_mode(mode_hint)
    expiry_dt = _next_weekly_expiry_from(now_dt)
    time_to_expiry_years = max((expiry_dt - now_dt).total_seconds(), 1.0) / (365 * 24 * 3600)

    try:
        sigma_default = float(os.getenv("SYNTHETIC_OPTIONS_IV", "0.22"))
    except Exception:
        sigma_default = 0.22
    sigma = max(0.05, min(sigma_default, 1.5))

    try:
        step = int(os.getenv("SYNTHETIC_OPTIONS_STRIKE_STEP", "100"))
    except Exception:
        step = 100

    risk_free = estimate_risk_free_rate() if callable(estimate_risk_free_rate) else 0.06

    center = int(round(spot / step) * step)
    width = 7  # strikes on each side
    strikes: List[Dict[str, Any]] = []
    total_call_oi = 0
    total_put_oi = 0

    for offset in range(-width, width + 1):
        strike_price = center + offset * step
        distance = abs(offset) or 1
        ce_price = black_scholes_price(spot, strike_price, time_to_expiry_years, risk_free, sigma, "call")
        pe_price = black_scholes_price(spot, strike_price, time_to_expiry_years, risk_free, sigma, "put")

        greeks_ce = calculate_option_greeks(spot, strike_price, time_to_expiry_years, risk_free, sigma, "call") if callable(calculate_option_greeks) else {}
        greeks_pe = calculate_option_greeks(spot, strike_price, time_to_expiry_years, risk_free, sigma, "put") if callable(calculate_option_greeks) else {}

        base_oi = max(int(2200 - distance * 160), 150)
        ce_oi = int(base_oi * (0.55 if offset >= 0 else 0.45))
        pe_oi = int(base_oi * (0.55 if offset <= 0 else 0.45))
        total_call_oi += ce_oi
        total_put_oi += pe_oi

        strikes.append({
            "strike": strike_price,
            "ce_ltp": round(ce_price, 2) if ce_price is not None else None,
            "ce_oi": ce_oi,
            "ce_volume": max(int(ce_oi * 0.12), 20),
            "ce_iv": round(sigma * 100, 2),
            "ce_delta": greeks_ce.get("delta"),
            "ce_gamma": greeks_ce.get("gamma"),
            "ce_theta": greeks_ce.get("theta"),
            "ce_vega": greeks_ce.get("vega"),
            "pe_ltp": round(pe_price, 2) if pe_price is not None else None,
            "pe_oi": pe_oi,
            "pe_volume": max(int(pe_oi * 0.12), 20),
            "pe_iv": round(sigma * 100, 2),
            "pe_delta": greeks_pe.get("delta"),
            "pe_gamma": greeks_pe.get("gamma"),
            "pe_theta": greeks_pe.get("theta"),
            "pe_vega": greeks_pe.get("vega"),
        })

    if not strikes:
        return None

    pcr = (total_put_oi / total_call_oi) if total_call_oi > 0 else None
    max_pain = max(strikes, key=lambda s: (s.get("ce_oi", 0) + s.get("pe_oi", 0)))

    return {
        "instrument": instrument,
        "expiry": expiry_dt.date().isoformat(),
        "strikes": strikes,
        "timestamp": now_dt.isoformat().replace("+00:00", "Z"),
        "futures_price": spot,
        "pcr": pcr,
        "max_pain": max_pain.get("strike") if isinstance(max_pain, dict) else None,
        "status": "synthetic",
        "mode_hint": mode_hint or _get_current_mode_hint(),
        "note": "Synthetic chain (Black-Scholes) for historical/offline mode",
    }


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return float(value)
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(x) for x in value]
    if isinstance(value, tuple):
        return [_json_safe_value(x) for x in value]
    return value


def _options_chain_has_liquidity(strikes: Any) -> bool:
    """Return True if any strike has non-zero OI/volume on either side."""
    if not isinstance(strikes, list):
        return False

    def _num(v: Any) -> float:
        try:
            if v is None:
                return 0.0
            return float(v)
        except Exception:
            return 0.0

    for strike in strikes:
        if not isinstance(strike, dict):
            continue
        ce = strike.get("CE") if isinstance(strike.get("CE"), dict) else {}
        pe = strike.get("PE") if isinstance(strike.get("PE"), dict) else {}

        vals = [
            strike.get("ce_oi"),
            strike.get("ce_volume"),
            strike.get("pe_oi"),
            strike.get("pe_volume"),
            ce.get("oi"),
            ce.get("volume"),
            pe.get("oi"),
            pe.get("volume"),
        ]
        if any(_num(v) > 0 for v in vals):
            return True
    return False


def _normalize_options_contract(
    instrument: str,
    payload: Optional[Dict[str, Any]],
    *,
    expiry: Optional[str] = None,
    mode_hint: Optional[str] = None,
    default_status: str = "ok",
) -> Dict[str, Any]:
    """Force a stable options payload shape for API/UI/agents."""
    out: Dict[str, Any] = dict(payload or {})
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    inst = str(out.get("instrument") or instrument or "").strip() or instrument
    resolved_mode = str(out.get("mode_hint") or mode_hint or _get_current_mode_hint() or "unknown").lower()
    source = out.get("source") or resolved_mode

    futures_price = _coerce_float(out.get("futures_price"))
    underlying_price = _coerce_float(out.get("underlying_price"))
    ref_price = _reference_price_for_options(inst)
    if futures_price is None and underlying_price is not None:
        futures_price = underlying_price
    if underlying_price is None and futures_price is not None:
        underlying_price = futures_price
    if futures_price is None and underlying_price is None and ref_price is not None:
        futures_price = ref_price
        underlying_price = ref_price

    strikes = out.get("strikes")
    if not isinstance(strikes, list):
        strikes = []

    out["status"] = str(out.get("status") or default_status)
    out["source"] = str(source)
    out["mode_hint"] = resolved_mode
    out["timestamp"] = _normalize_timestamp_string(out.get("timestamp")) or now_iso
    out["instrument"] = inst
    out["expiry"] = out.get("expiry") or expiry
    out["underlying_price"] = underlying_price
    out["futures_price"] = futures_price
    out["pcr"] = _coerce_float(out.get("pcr"))
    out["max_pain"] = out.get("max_pain")
    out["strikes"] = strikes

    return _normalize_timestamp_fields(out)


def _normalize_depth_contract(
    instrument: str,
    payload: Optional[Dict[str, Any]],
    *,
    mode_hint: Optional[str] = None,
    default_status: str = "ok",
) -> Dict[str, Any]:
    """Force a stable depth payload shape for API/UI/agents."""
    out: Dict[str, Any] = dict(payload or {})
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    inst = str(out.get("instrument") or instrument or "").strip() or instrument
    resolved_mode = str(out.get("mode_hint") or mode_hint or _get_current_mode_hint() or "unknown").lower()
    source = out.get("source") or resolved_mode

    underlying_price = _coerce_float(out.get("underlying_price"))
    futures_price = _coerce_float(out.get("futures_price"))
    ref_price = _reference_price_for_options(inst)
    if underlying_price is None and futures_price is not None:
        underlying_price = futures_price
    if underlying_price is None and ref_price is not None:
        underlying_price = ref_price

    buy = out.get("buy")
    sell = out.get("sell")
    if not isinstance(buy, list):
        buy = []
    if not isinstance(sell, list):
        sell = []

    out["status"] = str(out.get("status") or default_status)
    out["source"] = str(source)
    out["mode_hint"] = resolved_mode
    out["timestamp"] = _normalize_timestamp_string(out.get("timestamp")) or now_iso
    out["instrument"] = inst
    out["underlying_price"] = underlying_price
    out["buy"] = buy
    out["sell"] = sell

    return _normalize_timestamp_fields(out)


# ============================================================================
# WebSocket: STOMP over WebSocket + Redis Pub/Sub Bridge
# ============================================================================


def _serialize_stomp_frame(command: str, headers: Optional[Dict[str, str]] = None, body: str = "") -> str:
    """Serialize a STOMP frame for WebSocket transport."""
    headers = headers or {}
    lines = [command]
    for k, v in headers.items():
        lines.append(f"{k}:{v}")
    lines.append("")  # header/body separator
    lines.append(body or "")
    return "\n".join(lines) + "\x00"


def _parse_stomp_frames(buffer: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse any complete STOMP frames from buffer.

    Returns (frames, remainder_buffer).
    Each frame is a dict: {command, headers, body}.
    """
    frames: List[Dict[str, Any]] = []
    if not buffer:
        return frames, buffer

    parts = buffer.split("\x00")
    remainder = parts[-1]
    for raw in parts[:-1]:
        raw = raw.lstrip("\n")  # ignore heartbeats/newlines
        if not raw.strip():
            continue

        if "\n" not in raw:
            continue

        command, rest = raw.split("\n", 1)
        if "\n\n" in rest:
            header_blob, body = rest.split("\n\n", 1)
        else:
            header_blob, body = rest, ""

        headers: dict[str, str] = {}
        for line in header_blob.split("\n"):
            if not line.strip():
                continue
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()

        frames.append({"command": command.strip(), "headers": headers, "body": body})

    return frames, remainder


def _stomp_destination_to_redis(destination: str) -> List[Tuple[str, str]]:
    """Map a STOMP destination to one or more Redis pub/sub subscriptions.

    Returns list of (kind, name) where kind is 'channel' or 'pattern'.
    """
    # Auth status
    if destination == "/topic/auth/status":
        return [("channel", "auth:status")]

    # OHLC
    if destination.startswith("/topic/market/ohlc/"):
        parts = destination.split("/")
        # /topic/market/ohlc/{instrument}
        if len(parts) == 5:
            instrument = parts[4]
            return [("pattern", f"market:ohlc:{instrument}:*")]
        # /topic/market/ohlc/{instrument}/{timeframe}
        if len(parts) >= 6:
            instrument = parts[4]
            timeframe = parts[5]
            return [("channel", f"market:ohlc:{instrument}:{timeframe}")]

    # Indicators (publisher uses type-specific suffix: indicators:{symbol}:{type})
    if destination.startswith("/topic/indicators/"):
        instrument = destination.split("/", 3)[-1]
        return [("pattern", f"indicators:{instrument}:*")]

    # Ticks (publisher uses suffix: market:tick:{symbol}:{type})
    if destination.startswith("/topic/market/tick/"):
        instrument = destination.split("/", 4)[-1]
        return [("pattern", f"market:tick:{instrument}:*")]

    # Debug/raw access (exact Redis channel)
    if destination.startswith("/topic/raw/"):
        channel = destination.split("/", 3)[-1]
        return [("channel", channel)]

    return []


@app.websocket("/ws")
async def websocket_stomp(ws: WebSocket):
    """WebSocket endpoint that supports STOMP and bridges Redis pub/sub to browser."""
    requested_subprotocols = ws.scope.get("subprotocols") or []
    selected_subprotocol = next(
        (
            protocol
            for protocol in ("v12.stomp", "v11.stomp", "v10.stomp", "stomp")
            if protocol in requested_subprotocols
        ),
        None,
    )

    if selected_subprotocol:
        await ws.accept(subprotocol=selected_subprotocol)
    else:
        await ws.accept()

    conn_id = str(uuid.uuid4())
    mode: Optional[str] = None  # 'stomp' or 'legacy'
    stomp_connected = False
    message_seq = 0
    buffer = ""

    # NOTE: redis.asyncio pubsub is unreliable in some Windows setups.
    # Use sync Redis pubsub in a background thread and forward into this async WS.
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=0,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    loop = asyncio.get_running_loop()
    stop_event = threading.Event()
    ctrl_q: "queue.SimpleQueue[tuple[str, str]]" = queue.SimpleQueue()

    # STOMP subscriptions (internal_id -> {stomp_id, destination, kind, name})
    stomp_subs: Dict[str, Dict[str, str]] = {}
    # Legacy subscriptions (name -> {destination, kind, name})
    legacy_subs: Dict[str, Dict[str, str]] = {}

    async def _send_stomp_message(stomp_subscription_id: str, destination: str, payload: Dict[str, Any]):
        nonlocal message_seq
        message_seq += 1
        frame = _serialize_stomp_frame(
            "MESSAGE",
            headers={
                "subscription": stomp_subscription_id,
                "destination": destination,
                "message-id": f"{conn_id}:{message_seq}",
                "content-type": "application/json",
            },
            body=json.dumps(payload, ensure_ascii=False),
        )
        await ws.send_text(frame)

    def _channel_matches_pattern(channel_name: Any, pattern_glob: str) -> bool:
        """Return True if a Redis channel name matches a glob-style pattern.

        Redis PSUBSCRIBE patterns use glob semantics (e.g., market:ohlc:FOO:*).
        redis-py asyncio may not always provide msg['pattern'] consistently, so
        we defensively match against the channel string ourselves.
        """
        if not pattern_glob:
            return False
        try:
            ch = channel_name.decode("utf-8") if isinstance(channel_name, (bytes, bytearray)) else str(channel_name)
            return fnmatch.fnmatchcase(ch, pattern_glob)
        except Exception:
            return False

    async def _handle_redis_message(msg: Dict[str, Any]) -> None:
        """Handle a single Redis pub/sub message and forward to the WS client."""
        try:
            if not msg:
                return

            msg_type = msg.get("type")
            if msg_type not in {"message", "pmessage"}:
                return

            channel = msg.get("channel")
            data = msg.get("data")

            if not channel:
                return

            channel = channel.decode("utf-8") if isinstance(channel, (bytes, bytearray)) else str(channel)

            # Attempt JSON decode
            decoded: Any
            if isinstance(data, (bytes, bytearray)):
                try:
                    data = data.decode("utf-8")
                except Exception:
                    data = str(data)
            if isinstance(data, str):
                try:
                    decoded = json.loads(data)
                except Exception:
                    decoded = data
            else:
                decoded = data

            payload = {
                "type": "message",
                "channel": channel,
                "data": decoded,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            if mode == "legacy":
                for sub in list(legacy_subs.values()):
                    kind = sub.get("kind")
                    name = sub.get("name")
                    if kind == "channel" and name == channel:
                        await ws.send_text(json.dumps(payload, ensure_ascii=False))
                    elif kind == "pattern" and _channel_matches_pattern(channel, name):
                        await ws.send_text(json.dumps(payload, ensure_ascii=False))
                return

            for sub in list(stomp_subs.values()):
                kind = sub.get("kind")
                name = sub.get("name")
                if kind == "channel" and name == channel:
                    await _send_stomp_message(sub.get("stomp_id", ""), sub.get("destination", ""), payload)
                elif kind == "pattern" and _channel_matches_pattern(channel, name):
                    await _send_stomp_message(sub.get("stomp_id", ""), sub.get("destination", ""), payload)
        except Exception as e:
            logger.warning("WS forward error (%s): %s", conn_id, e)

    def _redis_thread() -> None:
        """Blocking Redis pubsub loop running in a background thread."""
        try:
            while not stop_event.is_set():
                # Apply any pending control commands
                while True:
                    try:
                        action, name = ctrl_q.get_nowait()
                    except Exception:
                        break
                    try:
                        if action == "subscribe":
                            pubsub.subscribe(name)
                        elif action == "psubscribe":
                            pubsub.psubscribe(name)
                        elif action == "unsubscribe":
                            pubsub.unsubscribe(name)
                        elif action == "punsubscribe":
                            pubsub.punsubscribe(name)
                    except Exception:
                        continue

                msg = pubsub.get_message(timeout=1.0)
                if msg:
                    try:
                        asyncio.run_coroutine_threadsafe(_handle_redis_message(msg), loop)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Redis WS thread ended (%s): %s", conn_id, e)

    t = threading.Thread(target=_redis_thread, name=f"ws-redis-{conn_id}", daemon=True)
    t.start()

    async def _legacy_subscribe(channels: list[str]):
        # Best-effort mapping from old dashboard channel list to actual Redis channels.
        for ch in channels:
            # already includes timeframe?
            if ch.startswith("market:ohlc:") and ch.count(":") == 2:
                # old: market:ohlc:{instrument} -> subscribe to all TF
                ctrl_q.put(("psubscribe", f"{ch}:*"))
                legacy_subs[ch] = {"destination": ch, "kind": "pattern", "name": f"{ch}:*"}
                continue
            if ch.startswith("indicators:") and ch.count(":") == 1:
                # old: indicators:{instrument} -> indicators:{instrument}:*
                pat = f"{ch}:*"
                ctrl_q.put(("psubscribe", pat))
                legacy_subs[ch] = {"destination": ch, "kind": "pattern", "name": pat}
                continue
            if ch.startswith("market:tick:") and ch.count(":") == 2:
                # old: market:tick:{instrument} -> market:tick:{instrument}:*
                pat = f"{ch}:*"
                ctrl_q.put(("psubscribe", pat))
                legacy_subs[ch] = {"destination": ch, "kind": "pattern", "name": pat}
                continue

            ctrl_q.put(("subscribe", ch))
            legacy_subs[ch] = {"destination": ch, "kind": "channel", "name": ch}

        await ws.send_text(json.dumps({"type": "subscribed", "channels": channels}, ensure_ascii=False))

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect()

            text = msg.get("text")
            if text is None:
                continue

            # Detect protocol mode on first message
            if mode is None:
                if text.lstrip().startswith("{"):
                    mode = "legacy"
                else:
                    mode = "stomp"

            if mode == "legacy":
                try:
                    data = json.loads(text)
                except Exception:
                    continue
                if data.get("action") == "subscribe":
                    channels = data.get("channels") or []
                    await ws.send_text(json.dumps({"type": "connected"}, ensure_ascii=False))
                    await _legacy_subscribe(channels)
                continue

            # STOMP
            buffer += text
            frames, buffer = _parse_stomp_frames(buffer)
            for frame in frames:
                command = frame.get("command")
                headers: dict[str, str] = frame.get("headers") or {}
                body: str = frame.get("body") or ""

                if command == "CONNECT":
                    stomp_connected = True
                    await ws.send_text(
                        _serialize_stomp_frame(
                            "CONNECTED",
                            headers={"version": "1.2", "heart-beat": "0,0"},
                            body="",
                        )
                    )
                    continue

                # Some clients send STOMP instead of CONNECT
                if command == "STOMP":
                    stomp_connected = True
                    await ws.send_text(
                        _serialize_stomp_frame(
                            "CONNECTED",
                            headers={"version": "1.2", "heart-beat": "0,0"},
                            body="",
                        )
                    )
                    continue

                if not stomp_connected:
                    # If client starts with SUBSCRIBE without CONNECT, be tolerant.
                    stomp_connected = True
                    await ws.send_text(
                        _serialize_stomp_frame(
                            "CONNECTED",
                            headers={"version": "1.2", "heart-beat": "0,0"},
                            body="",
                        )
                    )

                if command == "SUBSCRIBE":
                    destination = headers.get("destination", "")
                    sub_id = headers.get("id") or str(uuid.uuid4())

                    mapped = _stomp_destination_to_redis(destination)
                    if not mapped:
                        await ws.send_text(
                            _serialize_stomp_frame(
                                "ERROR",
                                headers={"message": f"Unknown destination: {destination}"},
                                body="",
                            )
                        )
                        continue

                    # One STOMP subscription can map to multiple Redis subscriptions.
                    # All MESSAGE frames must include the original STOMP subscription id.
                    for kind, name in mapped:
                        internal_id = str(uuid.uuid4())
                        stomp_subs[internal_id] = {
                            "stomp_id": sub_id,
                            "destination": destination,
                            "kind": kind,
                            "name": name,
                        }
                        if kind == "pattern":
                            ctrl_q.put(("psubscribe", name))
                        else:
                            ctrl_q.put(("subscribe", name))

                    receipt = headers.get("receipt")
                    if receipt:
                        await ws.send_text(_serialize_stomp_frame("RECEIPT", headers={"receipt-id": receipt}))
                    continue

                if command == "UNSUBSCRIBE":
                    sub_id = headers.get("id", "")
                    to_remove = [k for k, v in stomp_subs.items() if v.get("stomp_id") == sub_id]
                    for key in to_remove:
                        sub = stomp_subs.pop(key, None)
                        if not sub:
                            continue
                        if sub.get("kind") == "pattern":
                            ctrl_q.put(("punsubscribe", sub.get("name", "")))
                        else:
                            ctrl_q.put(("unsubscribe", sub.get("name", "")))
                    continue

                if command == "DISCONNECT":
                    receipt = headers.get("receipt")
                    if receipt:
                        await ws.send_text(_serialize_stomp_frame("RECEIPT", headers={"receipt-id": receipt}))
                    await ws.close()
                    return

                if command == "SEND":
                    # Optional: allow publishing to Redis from UI for debugging.
                    destination = headers.get("destination", "")
                    if destination.startswith("/app/redis/publish"):
                        redis_channel = headers.get("redis-channel")
                        if redis_channel:
                            await redis_client.publish(redis_channel, body)
                    continue

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("WebSocket error (%s): %s", conn_id, e)
    finally:
        try:
            stop_event.set()
        except Exception:
            pass
        try:
            pubsub.close()
        except Exception:
            pass
        try:
            redis_client.close()
        except Exception:
            pass

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Main dashboard page"""
    return templates.TemplateResponse("index.html", {"request": request})


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_trading_instance(value: Any) -> str:
    text = str(value or _TRADING_DEFAULT_INSTANCE).strip().lower()
    if not text:
        text = _TRADING_DEFAULT_INSTANCE
    safe = "".join(ch if (ch.isalnum() or ch in {"_", "-"}) else "_" for ch in text)
    safe = safe.strip("_")
    if not safe:
        safe = _TRADING_DEFAULT_INSTANCE
    return safe[:64]


def _trading_backtest_state_path(instance: str) -> Path:
    key = _normalize_trading_instance(instance)
    return _TRADING_BACKTEST_STATE_DIR / f"trading_backtest_latest_{key}.json"


def _save_latest_backtest_state(instance: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    key = _normalize_trading_instance(instance)
    safe_payload = _json_safe_value(payload)
    with _TRADING_LOCK:
        _TRADING_LAST_BACKTEST[key] = safe_payload
    try:
        _TRADING_BACKTEST_STATE_DIR.mkdir(parents=True, exist_ok=True)
        _trading_backtest_state_path(key).write_text(
            json.dumps(safe_payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return safe_payload


def _load_latest_backtest_state(instance: str) -> Optional[Dict[str, Any]]:
    key = _normalize_trading_instance(instance)
    with _TRADING_LOCK:
        cached = _TRADING_LAST_BACKTEST.get(key)
    if isinstance(cached, dict):
        return cached
    path = _trading_backtest_state_path(key)
    payload = _safe_load_json(path)
    if not isinstance(payload, dict):
        return None
    with _TRADING_LOCK:
        _TRADING_LAST_BACKTEST[key] = payload
    return payload


def _default_trading_paths(instance: str) -> Tuple[Path, Path, Path]:
    key = _normalize_trading_instance(instance)
    if key == _TRADING_DEFAULT_INSTANCE:
        return DEFAULT_TRADING_EVENTS_PATH, DEFAULT_TRADING_STDOUT_PATH, DEFAULT_TRADING_STDERR_PATH
    artifacts_dir = REPO_ROOT / "ml_pipeline" / "artifacts"
    return (
        artifacts_dir / f"t33_paper_capital_events_{key}.jsonl",
        artifacts_dir / f"t33_paper_capital_runner_{key}_stdout.log",
        artifacts_dir / f"t33_paper_capital_runner_{key}_stderr.log",
    )


def _get_trading_runner_state(instance: str) -> Dict[str, Any]:
    key = _normalize_trading_instance(instance)
    state = _TRADING_RUNNERS.get(key)
    if state is not None:
        return state
    events_path, stdout_path, stderr_path = _default_trading_paths(key)
    state = {
        "instance": key,
        "process": None,
        "stdout_handle": None,
        "stderr_handle": None,
        "started_at": None,
        "last_exit_code": None,
        "config": {},
        "events_path": events_path,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
    }
    _TRADING_RUNNERS[key] = state
    return state


def _close_trading_log_handles(state: Dict[str, Any]) -> None:
    for handle_key in ("stdout_handle", "stderr_handle"):
        handle = state.get(handle_key)
        try:
            if handle:
                handle.flush()
                handle.close()
        except Exception:
            pass
        state[handle_key] = None


def _refresh_trading_runner_state(instance: str) -> Dict[str, Any]:
    state = _get_trading_runner_state(instance)
    process = state.get("process")
    if process is None:
        return state
    rc = process.poll()
    if rc is None:
        return state
    state["last_exit_code"] = int(rc)
    state["process"] = None
    _close_trading_log_handles(state)
    return state


def _stop_trading_process_locked(state: Dict[str, Any], *, reason: str = "manual_stop") -> Dict[str, Any]:
    process = state.get("process")
    if process is None:
        return {
            "stopped": False,
            "reason": "not_running",
            "last_exit_code": state.get("last_exit_code"),
        }
    try:
        process.terminate()
        process.wait(timeout=8)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=3)
        except Exception:
            pass
    state["last_exit_code"] = process.poll()
    state["process"] = None
    _close_trading_log_handles(state)
    return {
        "stopped": True,
        "reason": reason,
        "last_exit_code": state.get("last_exit_code"),
    }


def _event_side(event: Dict[str, Any]) -> Optional[str]:
    pos = event.get("position")
    if isinstance(pos, dict):
        side = str(pos.get("side") or "").upper().strip()
        if side in {"CE", "PE"}:
            return side
    action = str(event.get("action") or "").upper().strip()
    if action == "BUY_CE":
        return "CE"
    if action == "BUY_PE":
        return "PE"
    return None


def _event_position_runtime(event: Dict[str, Any]) -> Dict[str, Any]:
    runtime = event.get("position_runtime")
    if isinstance(runtime, dict):
        return runtime
    # Backward compatibility: older emitters only carried "position".
    pos = event.get("position")
    if isinstance(pos, dict):
        return pos
    return {}


def _price_for_side(event: Dict[str, Any], side: Optional[str]) -> Optional[float]:
    if side not in {"CE", "PE"}:
        return None
    event_type = str(event.get("event_type") or "").upper().strip()
    position_runtime = _event_position_runtime(event)
    if position_runtime:
        # Entry price is explicitly captured for the held contract.
        if event_type == "ENTRY":
            runtime_entry_price = _coerce_float(position_runtime.get("entry_price"))
            if runtime_entry_price is not None:
                return runtime_entry_price
    risk = event.get("risk")
    if isinstance(risk, dict) and event_type == "EXIT":
        # Exit events can carry explicit fill/current price from stop handling.
        risk_exit_price = _coerce_float(risk.get("exit_price"))
        if risk_exit_price is None:
            risk_exit_price = _coerce_float(risk.get("current_price"))
        if risk_exit_price is not None:
            return risk_exit_price
    prices = event.get("prices")
    if not isinstance(prices, dict):
        return None
    key = "opt_0_ce_close" if side == "CE" else "opt_0_pe_close"
    return _coerce_float(prices.get(key))


def _load_trading_events(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    rows: List[Dict[str, Any]]
    if limit is not None and limit > 0:
        q: deque = deque(maxlen=int(limit))
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    q.append(obj)
        rows = list(q)
    else:
        rows = []
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    return rows


def _build_trading_state(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    initial_total = None
    final_total = None
    latest_prices: Dict[str, Optional[float]] = {"ce": None, "pe": None}
    first_ts = None
    last_ts = None
    ce_threshold = None
    pe_threshold = None

    if events:
        first_ts = events[0].get("timestamp")
        last_ts = events[-1].get("timestamp")
        first_cap = events[0].get("capital") if isinstance(events[0].get("capital"), dict) else {}
        last_cap = events[-1].get("capital") if isinstance(events[-1].get("capital"), dict) else {}
        initial_total = _coerce_float(first_cap.get("total_capital_mtm")) if isinstance(first_cap, dict) else None
        final_total = _coerce_float(last_cap.get("total_capital_mtm")) if isinstance(last_cap, dict) else None

        for event in reversed(events):
            prices = event.get("prices")
            if not isinstance(prices, dict):
                continue
            ce = _coerce_float(prices.get("opt_0_ce_close"))
            pe = _coerce_float(prices.get("opt_0_pe_close"))
            if latest_prices["ce"] is None and ce is not None:
                latest_prices["ce"] = ce
            if latest_prices["pe"] is None and pe is not None:
                latest_prices["pe"] = pe
            if latest_prices["ce"] is not None and latest_prices["pe"] is not None:
                break

        for event in reversed(events):
            if ce_threshold is None:
                ce_threshold = _coerce_float(event.get("ce_threshold"))
            if pe_threshold is None:
                pe_threshold = _coerce_float(event.get("pe_threshold"))
            if ce_threshold is not None and pe_threshold is not None:
                break

    trades: List[Dict[str, Any]] = []
    open_position: Optional[Dict[str, Any]] = None

    for event in events:
        event_type = str(event.get("event_type") or "").upper().strip()
        side = _event_side(event)
        ts = str(event.get("timestamp") or "")
        capital = event.get("capital") if isinstance(event.get("capital"), dict) else {}
        risk = event.get("risk") if isinstance(event.get("risk"), dict) else {}
        position_runtime = _event_position_runtime(event)
        total_capital = _coerce_float(capital.get("total_capital_mtm")) if isinstance(capital, dict) else None
        px = _price_for_side(event, side)
        runtime_entry_px = _coerce_float(position_runtime.get("entry_price")) if position_runtime else None
        entry_px = runtime_entry_px if runtime_entry_px is not None else px

        if event_type == "ENTRY" and side in {"CE", "PE"}:
            open_position = {
                "side": side,
                "entry_timestamp": ts,
                "entry_price": entry_px,
                "entry_total_capital": total_capital,
                "entry_confidence": _coerce_float((event.get("position") or {}).get("entry_confidence")) if isinstance(event.get("position"), dict) else None,
                "stop_price": _coerce_float(risk.get("stop_price")),
                "high_water_price": _coerce_float(risk.get("high_water_price")),
                "option_symbol": position_runtime.get("option_symbol"),
                "qty": _coerce_float(position_runtime.get("qty")),
                "lots_equivalent": _coerce_float(position_runtime.get("lots_equivalent")),
                "lot_size": _coerce_float(position_runtime.get("lot_size")),
                "atm_strike": position_runtime.get("atm_strike"),
                "expiry_code": position_runtime.get("expiry_code"),
            }
            continue

        if event_type == "MANAGE" and open_position is not None:
            open_position["stop_price"] = _coerce_float(risk.get("stop_price")) if risk else open_position.get("stop_price")
            open_position["high_water_price"] = (
                _coerce_float(risk.get("high_water_price")) if risk else open_position.get("high_water_price")
            )
            if position_runtime:
                open_position["option_symbol"] = position_runtime.get("option_symbol") or open_position.get("option_symbol")
                open_position["qty"] = _coerce_float(position_runtime.get("qty")) or open_position.get("qty")
                open_position["lots_equivalent"] = (
                    _coerce_float(position_runtime.get("lots_equivalent")) or open_position.get("lots_equivalent")
                )
                open_position["lot_size"] = _coerce_float(position_runtime.get("lot_size")) or open_position.get("lot_size")
            continue

        if event_type == "EXIT" and open_position is not None:
            exit_side = side or str(open_position.get("side") or "")
            exit_price = _price_for_side(event, exit_side)
            entry_price = _coerce_float(open_position.get("entry_price"))
            return_pct = None
            if entry_price and exit_price is not None and entry_price > 0:
                return_pct = ((float(exit_price) - float(entry_price)) / float(entry_price)) * 100.0
            pnl = None
            entry_total = _coerce_float(open_position.get("entry_total_capital"))
            if total_capital is not None and entry_total is not None:
                pnl = float(total_capital) - float(entry_total)
            trades.append(
                {
                    "side": open_position.get("side"),
                    "option_symbol": open_position.get("option_symbol"),
                    "qty": _coerce_float(open_position.get("qty")),
                    "lots_equivalent": _coerce_float(open_position.get("lots_equivalent")),
                    "lot_size": _coerce_float(open_position.get("lot_size")),
                    "entry_timestamp": open_position.get("entry_timestamp"),
                    "exit_timestamp": ts,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "buy_value": (
                        float(entry_price) * float(open_position.get("qty"))
                        if entry_price is not None and _coerce_float(open_position.get("qty")) is not None
                        else None
                    ),
                    "sell_value": (
                        float(exit_price) * float(open_position.get("qty"))
                        if exit_price is not None and _coerce_float(open_position.get("qty")) is not None
                        else None
                    ),
                    "return_pct": return_pct,
                    "pnl": pnl,
                    "exit_reason": event.get("event_reason"),
                    "held_minutes": event.get("held_minutes"),
                    "stop_price": _coerce_float(risk.get("stop_price")),
                }
            )
            open_position = None

    if open_position is not None:
        side = str(open_position.get("side") or "")
        live_price = latest_prices["ce"] if side == "CE" else latest_prices["pe"] if side == "PE" else None
        open_position["live_price"] = live_price
        entry_price = _coerce_float(open_position.get("entry_price"))
        qty = _coerce_float(open_position.get("qty"))
        if entry_price and live_price is not None and entry_price > 0:
            open_position["live_return_pct"] = ((float(live_price) - float(entry_price)) / float(entry_price)) * 100.0
        else:
            open_position["live_return_pct"] = None
        if live_price is not None and qty is not None:
            open_position["market_value"] = float(live_price) * float(qty)
        else:
            open_position["market_value"] = None

    wins = 0
    for trade in trades:
        pnl = _coerce_float(trade.get("pnl"))
        ret = _coerce_float(trade.get("return_pct"))
        if pnl is not None and pnl > 0:
            wins += 1
        elif pnl is None and ret is not None and ret > 0:
            wins += 1

    total_trades = len(trades)
    win_rate = ((wins / total_trades) * 100.0) if total_trades > 0 else 0.0
    profit = (float(final_total) - float(initial_total)) if (final_total is not None and initial_total is not None) else None

    recent_events: List[Dict[str, Any]] = []
    for event in events[-25:]:
        position_runtime = _event_position_runtime(event)
        option_symbol = (
            position_runtime.get("option_symbol")
            or event.get("option_symbol")
            or event.get("contract")
        )
        instrument = (
            position_runtime.get("instrument")
            or event.get("instrument")
        )
        atm_strike = _coerce_float(position_runtime.get("atm_strike"))
        if atm_strike is None:
            atm_strike = _coerce_float(event.get("atm_strike"))
        expiry_code = (
            position_runtime.get("expiry_code")
            or event.get("expiry_code")
        )
        recent_events.append(
            {
                "timestamp": event.get("timestamp"),
                "generated_at": event.get("generated_at"),
                "event_type": event.get("event_type"),
                "event_reason": event.get("event_reason"),
                "action": event.get("action"),
                "side": _event_side(event),
                "ce_prob": _coerce_float(event.get("ce_prob")),
                "pe_prob": _coerce_float(event.get("pe_prob")),
                "stop_price": _coerce_float((event.get("risk") or {}).get("stop_price")) if isinstance(event.get("risk"), dict) else None,
                "instrument": instrument,
                "contract": option_symbol,
                "option_symbol": option_symbol,
                "atm_strike": atm_strike,
                "expiry_code": expiry_code,
                "qty": _coerce_float(position_runtime.get("qty")),
                "total_capital_mtm": _coerce_float((event.get("capital") or {}).get("total_capital_mtm")) if isinstance(event.get("capital"), dict) else None,
            }
        )

    signal_series: List[Dict[str, Any]] = []
    for event in events[-180:]:
        position_runtime = _event_position_runtime(event)
        option_symbol = (
            position_runtime.get("option_symbol")
            or event.get("option_symbol")
            or event.get("contract")
        )
        instrument = (
            position_runtime.get("instrument")
            or event.get("instrument")
        )
        atm_strike = _coerce_float(position_runtime.get("atm_strike"))
        if atm_strike is None:
            atm_strike = _coerce_float(event.get("atm_strike"))
        expiry_code = (
            position_runtime.get("expiry_code")
            or event.get("expiry_code")
        )
        signal_series.append(
            {
                "timestamp": event.get("timestamp"),
                "event_type": event.get("event_type"),
                "action": event.get("action"),
                "side": _event_side(event),
                "ce_prob": _coerce_float(event.get("ce_prob")),
                "pe_prob": _coerce_float(event.get("pe_prob")),
                "ce_threshold": _coerce_float(event.get("ce_threshold")),
                "pe_threshold": _coerce_float(event.get("pe_threshold")),
                "instrument": instrument,
                "contract": option_symbol,
                "option_symbol": option_symbol,
                "atm_strike": atm_strike,
                "expiry_code": expiry_code,
            }
        )

    return {
        "summary": {
            "events_count": len(events),
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "initial_total_capital": initial_total,
            "final_total_capital": final_total,
            "profit": profit,
            "ce_threshold": ce_threshold,
            "pe_threshold": pe_threshold,
            "total_trades": total_trades,
            "wins": wins,
            "losses": max(total_trades - wins, 0),
            "win_rate_pct": win_rate,
            "latest_ce_price": latest_prices["ce"],
            "latest_pe_price": latest_prices["pe"],
        },
        "open_position": open_position,
        "trades": list(reversed(trades[-50:])),
        "recent_events": list(reversed(recent_events)),
        "signal_series": signal_series,
    }


@app.get("/trading", response_class=HTMLResponse)
async def trading_terminal(request: Request):
    """Trading operator terminal UI."""
    query = dict(request.query_params)
    model_key_raw = str(query.get("model") or "").strip()
    if model_key_raw:
        safe_key = _normalize_trading_instance(model_key_raw)
        catalog = _build_trading_model_catalog()
        selected = next(
            (
                item
                for item in catalog
                if str(item.get("instance_key") or "").strip().lower() == safe_key.lower()
            ),
            None,
        )
        if isinstance(selected, dict):
            merged = dict(query)
            changed = False
            for key in ("model_package", "threshold_report", "eval_summary_path", "training_report_path"):
                if not str(merged.get(key) or "").strip():
                    value = str(selected.get(key) or "").strip()
                    if value:
                        merged[key] = value
                        changed = True
            merged["model"] = safe_key
            if changed or safe_key != model_key_raw:
                return RedirectResponse(url=f"/trading?{urlencode(merged)}", status_code=307)
    return templates.TemplateResponse("trading_terminal.html", {"request": request})


@app.get("/trading/models", response_class=HTMLResponse)
async def trading_models_page(request: Request):
    """Model catalog page for choosing a trading model/profile."""
    models = _build_trading_model_catalog()
    return templates.TemplateResponse(
        "trading_models.html",
        {
            "request": request,
            "models": models,
            "summary": {
                "total": len(models),
                "ready": sum(1 for m in models if m.get("ready_to_run")),
                "recommended": sum(1 for m in models if m.get("recommended")),
            },
        },
    )


@app.get("/api/trading/models")
async def get_trading_models():
    """Machine-readable catalog of configured and discovered model artifacts."""
    models = _build_trading_model_catalog()
    return {
        "status": "ok",
        "count": len(models),
        "ready_count": sum(1 for m in models if m.get("ready_to_run")),
        "models": models,
    }


@app.get("/trading/model/{model_key}")
async def trading_terminal_model(model_key: str):
    """Convenience route for model-scoped terminal tabs."""
    safe_key = _normalize_trading_instance(model_key)
    for entry in _build_trading_model_catalog():
        if str(entry.get("instance_key") or "").strip().lower() == safe_key.lower():
            prefill_url = str(entry.get("prefill_url") or "").strip()
            if prefill_url:
                return RedirectResponse(url=prefill_url, status_code=307)
            break
    return RedirectResponse(url=f"/trading?model={safe_key}", status_code=307)


@app.get("/api/trading/model-evaluation")
async def get_trading_model_evaluation(
    summary_path: Optional[str] = None,
    training_report_path: Optional[str] = None,
    policy_report_path: Optional[str] = None,
):
    """Return model quality snapshot for UI (OOS, training, and policy metadata)."""
    summary_file = _resolve_repo_path(summary_path, DEFAULT_MODEL_EVAL_SUMMARY_PATH)
    training_file = _resolve_repo_path(training_report_path, DEFAULT_MODEL_TRAINING_REPORT_PATH)
    policy_file = _resolve_repo_path(policy_report_path, DEFAULT_MODEL_POLICY_REPORT_PATH)
    snapshot = _build_model_eval_snapshot(summary_file, training_file, policy_file)
    snapshot["status"] = "ok"
    return snapshot


@app.post("/api/trading/backtest/run")
async def run_trading_backtest(request: Request):
    """Run one-date backtest using selected model artifacts (auto source: local archive or Mongo)."""
    payload: Dict[str, Any] = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            payload = body
    except Exception:
        payload = {}

    backtest_date = str(payload.get("date") or "").strip()
    if not backtest_date:
        raise HTTPException(status_code=400, detail="date is required (YYYY-MM-DD)")
    try:
        datetime.strptime(backtest_date, "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    instrument = str(payload.get("instrument") or "").strip().upper()
    if not instrument:
        raise HTTPException(status_code=400, detail="instrument is required")

    model_path = _resolve_repo_path(str(payload.get("model_package") or "").strip())
    threshold_path = _resolve_repo_path(str(payload.get("threshold_report") or "").strip())
    ce_threshold = _coerce_float(payload.get("ce_threshold"))
    pe_threshold = _coerce_float(payload.get("pe_threshold"))
    if not isinstance(model_path, Path) or not model_path.exists():
        raise HTTPException(status_code=400, detail=f"model package not found: {model_path}")
    if not isinstance(threshold_path, Path) or not threshold_path.exists():
        raise HTTPException(status_code=400, detail=f"threshold report not found: {threshold_path}")
    if ce_threshold is not None and (ce_threshold < 0.0 or ce_threshold > 1.0):
        raise HTTPException(status_code=400, detail=f"ce_threshold must be within [0, 1], got {ce_threshold}")
    if pe_threshold is not None and (pe_threshold < 0.0 or pe_threshold > 1.0):
        raise HTTPException(status_code=400, detail=f"pe_threshold must be within [0, 1], got {pe_threshold}")

    source = str(payload.get("source") or "auto").strip().lower()
    if source not in {"auto", "local", "mongo"}:
        raise HTTPException(status_code=400, detail="source must be one of: auto, local, mongo")

    base_path = str(payload.get("base_path") or "").strip()
    mongo_uri = str(payload.get("mongo_uri") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017/").strip()
    mongo_db = str(payload.get("mongo_db") or os.getenv("MONGO_DB") or "trading_ai").strip()
    vix_path = str(payload.get("vix_path") or "").strip()
    t19_path = _resolve_repo_path(str(payload.get("t19_report") or "").strip()) if payload.get("t19_report") else None
    out_dir_rel = str(payload.get("out_dir") or "ml_pipeline/artifacts/backtest_runs").strip()
    out_dir = Path(out_dir_rel) if Path(out_dir_rel).is_absolute() else (REPO_ROOT / out_dir_rel)
    instance_key = _normalize_trading_instance(payload.get("instance"))

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_instrument = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in instrument)
    run_tag = str(payload.get("tag") or f"{backtest_date}_{safe_instrument}_{run_id}")

    env = dict(os.environ)
    current_pythonpath = str(env.get("PYTHONPATH") or "").strip()
    if current_pythonpath:
        env["PYTHONPATH"] = f"{ML_PIPELINE_SRC}{os.pathsep}{current_pythonpath}"
    else:
        env["PYTHONPATH"] = str(ML_PIPELINE_SRC)
    auto_refresh_vix = _truthy(payload.get("auto_refresh_vix"), default=True)
    if auto_refresh_vix:
        env["ML_PIPELINE_AUTO_FETCH_VIX"] = "1"
        env["ML_PIPELINE_VIX_FROM_DATE"] = str(payload.get("vix_from_date") or "2024-01-01").strip()

    cmd = [
        sys.executable,
        "-m",
        "ml_pipeline.date_backtest_runner",
        "--date",
        backtest_date,
        "--instrument",
        instrument,
        "--model-package",
        str(model_path),
        "--threshold-report",
        str(threshold_path),
        "--source",
        source,
        "--mongo-uri",
        mongo_uri,
        "--mongo-db",
        mongo_db,
        "--out-dir",
        str(out_dir),
        "--tag",
        run_tag,
    ]
    if base_path:
        cmd.extend(["--base-path", base_path])
    if vix_path:
        cmd.extend(["--vix-path", vix_path])
    if isinstance(t19_path, Path):
        cmd.extend(["--t19-report", str(t19_path)])
    if ce_threshold is not None:
        cmd.extend(["--ce-threshold", str(float(ce_threshold))])
    if pe_threshold is not None:
        cmd.extend(["--pe-threshold", str(float(pe_threshold))])

    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if len(detail) > 1800:
            detail = detail[-1800:]
        raise HTTPException(status_code=500, detail=f"backtest failed: {detail}")

    full_report_path: Optional[Path] = None
    for line in (proc.stdout or "").splitlines():
        text = str(line).strip()
        if text.startswith("FULL_REPORT="):
            candidate = text.split("=", 1)[1].strip()
            if candidate:
                full_report_path = Path(candidate)
                break
    if full_report_path is None:
        expected = out_dir / run_tag / "full_report.json"
        if expected.exists():
            full_report_path = expected
    if full_report_path is None or not full_report_path.exists():
        raise HTTPException(status_code=500, detail="backtest completed but full report was not found")

    try:
        result = json.loads(full_report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to parse backtest report: {exc}")

    # Build a UI-ready snapshot from backtest decisions so terminal tables/charts
    # can switch from live runner state to this run immediately.
    ui_state: Dict[str, Any] = {}
    try:
        decisions_path_raw = (
            ((result.get("artifacts") or {}).get("decisions_jsonl"))
            if isinstance(result, dict)
            else None
        )
        decisions_path = Path(str(decisions_path_raw)) if decisions_path_raw else None
        if isinstance(decisions_path, Path) and decisions_path.exists():
            events = _load_trading_events(decisions_path, limit=5000)
            ui_state = _build_trading_state(events)
            ui_state["runner"] = {
                "instance": _normalize_trading_instance(payload.get("instance")),
                "running": False,
                "pid": None,
                "started_at": None,
                "last_exit_code": 0,
                "config": {
                    "instrument": instrument,
                    "model_package": str(model_path),
                    "threshold_report": str(threshold_path),
                    "ce_threshold": float(ce_threshold) if ce_threshold is not None else None,
                    "pe_threshold": float(pe_threshold) if pe_threshold is not None else None,
                    "mode": "backtest",
                },
                "events_path": str(decisions_path),
                "view_mode": "backtest",
                "backtest_date": backtest_date,
                "backtest_run_tag": run_tag,
            }
    except Exception:
        ui_state = {}

    response_payload = {
        "status": "ok",
        "instance": instance_key,
        "run_tag": run_tag,
        "report_path": str(full_report_path),
        "result": result,
        "ui_state": ui_state,
    }
    safe_payload = _json_safe_value(response_payload)
    _save_latest_backtest_state(
        instance_key,
        {
            "instance": instance_key,
            "run_tag": run_tag,
            "report_path": str(full_report_path),
            "created_at": datetime.now(IST_TZ).isoformat(),
            "ui_state": safe_payload.get("ui_state") if isinstance(safe_payload, dict) else {},
        },
    )
    return safe_payload


@app.get("/api/trading/backtest/latest")
async def get_latest_backtest_state(instance: Optional[str] = None):
    instance_key = _normalize_trading_instance(instance)
    latest = _load_latest_backtest_state(instance_key)
    if not isinstance(latest, dict):
        return {"status": "not_found", "instance": instance_key}
    return {"status": "ok", **latest}


@app.get("/api/trading/state")
async def get_trading_state(
    limit: int = 2000,
    instance: Optional[str] = None,
    view: Optional[str] = None,
):
    """Get paper trading runner status + capital/positions/trades from JSONL output."""
    instance_key = _normalize_trading_instance(instance)
    with _TRADING_LOCK:
        state = _refresh_trading_runner_state(instance_key)
        process = state.get("process")
        runner_pid = process.pid if process is not None else None
        runner_running = (process is not None and process.poll() is None)
        started_at = state.get("started_at")
        last_exit = state.get("last_exit_code")
        runner_cfg = dict(state.get("config") or {})
        events_path = state.get("events_path")
        if not isinstance(events_path, Path):
            events_path = Path(str(events_path or DEFAULT_TRADING_EVENTS_PATH))

    view_mode = str(view or "auto").strip().lower()
    if view_mode in {"backtest", "latest_backtest"}:
        latest = _load_latest_backtest_state(instance_key)
        ui_state = latest.get("ui_state") if isinstance(latest, dict) else None
        if isinstance(ui_state, dict):
            return ui_state

    events = _load_trading_events(events_path, limit=max(1, int(limit)))
    if (not runner_running) and len(events) == 0:
        latest = _load_latest_backtest_state(instance_key)
        ui_state = latest.get("ui_state") if isinstance(latest, dict) else None
        if isinstance(ui_state, dict):
            return ui_state

    payload = _build_trading_state(events)
    payload["runner"] = {
        "instance": instance_key,
        "running": runner_running,
        "pid": runner_pid,
        "started_at": started_at,
        "last_exit_code": last_exit,
        "config": runner_cfg,
        "events_path": str(events_path),
    }
    return payload


@app.post("/api/trading/start")
async def start_trading_runner(request: Request):
    """Start paper capital runner in background."""
    payload: Dict[str, Any] = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            payload = body
    except Exception:
        payload = {}
    instance = _normalize_trading_instance(payload.get("instance"))

    mode = str(payload.get("mode") or "dual").strip().lower()
    if mode not in {"dual", "ce_only", "pe_only"}:
        raise HTTPException(status_code=400, detail="mode must be one of: dual, ce_only, pe_only")

    requested_instrument = str(payload.get("instrument") or "").strip().upper()
    if requested_instrument and not _is_placeholder_instrument(requested_instrument):
        instrument = requested_instrument
    else:
        runtime_instruments = await _load_runtime_instruments(max_instruments=25)
        if DEFAULT_INSTRUMENT and not _is_placeholder_instrument(DEFAULT_INSTRUMENT):
            runtime_instruments = [str(DEFAULT_INSTRUMENT)] + list(runtime_instruments or [])
        instrument = str(
            _select_most_active_instrument(runtime_instruments, preferred_mode="live") or ""
        ).strip().upper()
        if not instrument:
            instrument = "BANKNIFTY-I"
        logger.info(
            "[trading/start] Auto-selected instrument=%s (requested=%s)",
            instrument,
            requested_instrument or "<empty>",
        )
    redis_host = str(payload.get("redis_host") or REDIS_HOST).strip()
    redis_port = int(payload.get("redis_port") or REDIS_PORT)
    redis_db = int(payload.get("redis_db") or 0)
    initial_ce_capital = float(payload.get("initial_ce_capital") or 1000.0)
    initial_pe_capital = float(payload.get("initial_pe_capital") or 1000.0)
    fee_bps = float(payload.get("fee_bps") or 5.0)
    max_iterations = int(payload.get("max_iterations") or 800)
    max_hold_minutes = int(payload.get("max_hold_minutes") or 5)
    confidence_buffer = float(payload.get("confidence_buffer") or 0.05)
    max_idle_seconds = float(payload.get("max_idle_seconds") or 300.0)
    stop_loss_pct = float(payload.get("stop_loss_pct") or 0.0)
    trailing_enabled = _truthy(payload.get("trailing_enabled"), default=False)
    trailing_activation_pct = float(payload.get("trailing_activation_pct") or 10.0)
    trailing_offset_pct = float(payload.get("trailing_offset_pct") or 5.0)
    trailing_lock_breakeven = _truthy(payload.get("trailing_lock_breakeven"), default=True)
    model_exit_policy = str(payload.get("model_exit_policy") or "strict").strip().lower()
    stagnation_enabled = _truthy(payload.get("stagnation_enabled"), default=False)
    _stagnation_window_raw = payload.get("stagnation_window_minutes")
    _stagnation_threshold_raw = payload.get("stagnation_threshold_pct")
    _stagnation_vol_mult_raw = payload.get("stagnation_volatility_multiplier")
    _stagnation_min_hold_raw = payload.get("stagnation_min_hold_minutes")
    stagnation_window_minutes = int(10 if _stagnation_window_raw in (None, "") else _stagnation_window_raw)
    stagnation_threshold_pct = float(0.8 if _stagnation_threshold_raw in (None, "") else _stagnation_threshold_raw)
    stagnation_volatility_multiplier = float(2.0 if _stagnation_vol_mult_raw in (None, "") else _stagnation_vol_mult_raw)
    stagnation_min_hold_minutes = int(0 if _stagnation_min_hold_raw in (None, "") else _stagnation_min_hold_raw)
    stop_execution_mode = str(payload.get("stop_execution_mode") or "stop_market").strip().lower()
    stop_limit_offset_pct = float(payload.get("stop_limit_offset_pct") or 0.2)
    stop_limit_max_wait_events = int(payload.get("stop_limit_max_wait_events") or 3)
    runtime_guard_max_consecutive_losses = int(payload.get("runtime_guard_max_consecutive_losses") or 0)
    runtime_guard_max_drawdown_pct = float(payload.get("runtime_guard_max_drawdown_pct") or 0.0)
    quality_max_entries_per_day = int(payload.get("quality_max_entries_per_day") or 0)
    quality_entry_cutoff_hour = int(payload.get("quality_entry_cutoff_hour") or -1)
    quality_entry_cooldown_minutes = int(payload.get("quality_entry_cooldown_minutes") or 0)
    quality_min_side_prob = float(payload.get("quality_min_side_prob") or 0.0)
    quality_min_prob_edge = float(payload.get("quality_min_prob_edge") or 0.0)
    quality_skip_weekdays = str(payload.get("quality_skip_weekdays") or "")
    option_lot_size = float(payload.get("option_lot_size") or 15.0)
    fresh_start = _truthy(payload.get("fresh_start"), default=True)
    restart_if_running = _truthy(payload.get("restart_if_running"), default=True)

    if model_exit_policy not in {"strict", "signal_only", "stop_only", "training_parity"}:
        raise HTTPException(
            status_code=400,
            detail="model_exit_policy must be one of: strict, signal_only, stop_only, training_parity",
        )
    if stop_execution_mode not in {"stop_market", "stop_limit"}:
        raise HTTPException(status_code=400, detail="stop_execution_mode must be one of: stop_market, stop_limit")

    default_events_path, default_stdout_path, default_stderr_path = _default_trading_paths(instance)
    model_rel = str(
        payload.get("model_package")
        or "ml_pipeline/artifacts/models/by_features/core_v2/h5_ts0_lgbm_regime/model/model.joblib"
    )
    threshold_rel = str(
        payload.get("threshold_report")
        or "ml_pipeline/artifacts/models/by_features/core_v2/h5_ts0_lgbm_regime/config/profiles/openfe_v9_dual/threshold_report.json"
    )
    output_raw = payload.get("output_jsonl")
    output_rel = str(output_raw).strip() if output_raw is not None else ""
    trace_raw = payload.get("feature_trace_jsonl")
    trace_rel = str(trace_raw).strip() if trace_raw is not None else ""

    model_path = Path(model_rel) if Path(model_rel).is_absolute() else (REPO_ROOT / model_rel)
    threshold_path = Path(threshold_rel) if Path(threshold_rel).is_absolute() else (REPO_ROOT / threshold_rel)
    ce_threshold = _coerce_float(payload.get("ce_threshold"))
    pe_threshold = _coerce_float(payload.get("pe_threshold"))
    if output_rel:
        output_path = Path(output_rel) if Path(output_rel).is_absolute() else (REPO_ROOT / output_rel)
    else:
        output_path = default_events_path
    if trace_rel:
        feature_trace_path = Path(trace_rel) if Path(trace_rel).is_absolute() else (REPO_ROOT / trace_rel)
    else:
        feature_trace_path = output_path.parent / f"t33_paper_feature_trace_{instance}.jsonl"
    stdout_path = default_stdout_path
    stderr_path = default_stderr_path

    if not model_path.exists():
        raise HTTPException(status_code=400, detail=f"model package not found: {model_path}")
    if not threshold_path.exists():
        raise HTTPException(status_code=400, detail=f"threshold report not found: {threshold_path}")
    if ce_threshold is not None and (ce_threshold < 0.0 or ce_threshold > 1.0):
        raise HTTPException(status_code=400, detail=f"ce_threshold must be within [0, 1], got {ce_threshold}")
    if pe_threshold is not None and (pe_threshold < 0.0 or pe_threshold > 1.0):
        raise HTTPException(status_code=400, detail=f"pe_threshold must be within [0, 1], got {pe_threshold}")

    requested_identity = {
        "instance": instance,
        "mode": mode,
        "instrument": instrument,
        "redis_host": redis_host,
        "redis_port": redis_port,
        "redis_db": redis_db,
        "initial_ce_capital": initial_ce_capital,
        "initial_pe_capital": initial_pe_capital,
        "fee_bps": fee_bps,
        "max_iterations": max_iterations,
        "max_hold_minutes": max_hold_minutes,
        "confidence_buffer": confidence_buffer,
        "max_idle_seconds": max_idle_seconds,
        "stop_loss_pct": stop_loss_pct,
        "trailing_enabled": trailing_enabled,
        "trailing_activation_pct": trailing_activation_pct,
        "trailing_offset_pct": trailing_offset_pct,
        "trailing_lock_breakeven": trailing_lock_breakeven,
        "model_exit_policy": model_exit_policy,
        "stagnation_enabled": bool(stagnation_enabled),
        "stagnation_window_minutes": max(2, int(stagnation_window_minutes)),
        "stagnation_threshold_pct": max(0.0, float(stagnation_threshold_pct)),
        "stagnation_volatility_multiplier": max(0.0, float(stagnation_volatility_multiplier)),
        "stagnation_min_hold_minutes": max(0, int(stagnation_min_hold_minutes)),
        "stop_execution_mode": stop_execution_mode,
        "stop_limit_offset_pct": max(0.0, float(stop_limit_offset_pct)),
        "stop_limit_max_wait_events": max(1, int(stop_limit_max_wait_events)),
        "runtime_guard_max_consecutive_losses": max(0, int(runtime_guard_max_consecutive_losses)),
        "runtime_guard_max_drawdown_pct": max(0.0, float(runtime_guard_max_drawdown_pct)),
        "quality_max_entries_per_day": max(0, int(quality_max_entries_per_day)),
        "quality_entry_cutoff_hour": int(quality_entry_cutoff_hour),
        "quality_entry_cooldown_minutes": max(0, int(quality_entry_cooldown_minutes)),
        "quality_min_side_prob": min(1.0, max(0.0, float(quality_min_side_prob))),
        "quality_min_prob_edge": min(1.0, max(0.0, float(quality_min_prob_edge))),
        "quality_skip_weekdays": quality_skip_weekdays,
        "option_lot_size": max(1.0, float(option_lot_size)),
        "model_package": str(model_path),
        "threshold_report": str(threshold_path),
        "ce_threshold": (float(ce_threshold) if ce_threshold is not None else None),
        "pe_threshold": (float(pe_threshold) if pe_threshold is not None else None),
        "output_jsonl": str(output_path),
        "feature_trace_jsonl": str(feature_trace_path),
    }

    with _TRADING_LOCK:
        state = _refresh_trading_runner_state(instance)
        current_process = state.get("process")
        restart_meta: Optional[Dict[str, Any]] = None
        if current_process is not None and current_process.poll() is None:
            current_cfg = dict(state.get("config") or {})
            changed_keys = sorted([k for k, v in requested_identity.items() if current_cfg.get(k) != v])
            if not restart_if_running and len(changed_keys) == 0:
                return {
                    "status": "already_running",
                    "instance": instance,
                    "pid": current_process.pid,
                    "events_path": str(state.get("events_path") or output_path),
                    "config": dict(state.get("config") or {}),
                }
            if restart_if_running or len(changed_keys) > 0:
                restart_meta = _stop_trading_process_locked(
                    state,
                    reason="restart_with_new_config" if len(changed_keys) > 0 else "restart_requested",
                )
                restart_meta["changed_keys"] = changed_keys
                current_process = None
                state = _refresh_trading_runner_state(instance)

        if current_process is not None and current_process.poll() is None:
            return {
                "status": "already_running",
                "instance": instance,
                "pid": current_process.pid,
                "events_path": str(state.get("events_path") or output_path),
                "config": dict(state.get("config") or {}),
            }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        feature_trace_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)

        if fresh_start:
            try:
                if output_path.exists():
                    output_path.unlink()
            except Exception:
                pass
            try:
                if stdout_path.exists():
                    stdout_path.unlink()
            except Exception:
                pass
            try:
                if stderr_path.exists():
                    stderr_path.unlink()
            except Exception:
                pass
            try:
                if feature_trace_path.exists():
                    feature_trace_path.unlink()
            except Exception:
                pass

        env = dict(os.environ)
        current_pythonpath = str(env.get("PYTHONPATH") or "").strip()
        if current_pythonpath:
            env["PYTHONPATH"] = f"{ML_PIPELINE_SRC}{os.pathsep}{current_pythonpath}"
        else:
            env["PYTHONPATH"] = str(ML_PIPELINE_SRC)
        auto_refresh_vix = _truthy(payload.get("auto_refresh_vix"), default=True)
        if auto_refresh_vix:
            env["ML_PIPELINE_AUTO_FETCH_VIX"] = "1"
            env["ML_PIPELINE_VIX_FROM_DATE"] = str(payload.get("vix_from_date") or "2024-01-01").strip()

        cmd = [
            sys.executable,
            "-m",
            "ml_pipeline.paper_capital_runner",
            "--mode",
            mode,
            "--instrument",
            instrument,
            "--model-package",
            str(model_path),
            "--threshold-report",
            str(threshold_path),
            "--redis-host",
            redis_host,
            "--redis-port",
            str(redis_port),
            "--redis-db",
            str(redis_db),
            "--initial-ce-capital",
            str(initial_ce_capital),
            "--initial-pe-capital",
            str(initial_pe_capital),
            "--fee-bps",
            str(fee_bps),
            "--max-iterations",
            str(max_iterations),
            "--max-hold-minutes",
            str(max_hold_minutes),
            "--confidence-buffer",
            str(confidence_buffer),
            "--max-idle-seconds",
            str(max_idle_seconds),
            "--stop-loss-pct",
            str(max(0.0, float(stop_loss_pct))),
            "--trailing-activation-pct",
            str(max(0.0, float(trailing_activation_pct))),
            "--trailing-offset-pct",
            str(max(0.0, float(trailing_offset_pct))),
            "--model-exit-policy",
            model_exit_policy,
            "--stagnation-window-minutes",
            str(max(2, int(stagnation_window_minutes))),
            "--stagnation-threshold-pct",
            str(max(0.0, float(stagnation_threshold_pct))),
            "--stagnation-volatility-multiplier",
            str(max(0.0, float(stagnation_volatility_multiplier))),
            "--stagnation-min-hold-minutes",
            str(max(0, int(stagnation_min_hold_minutes))),
            "--stop-execution-mode",
            stop_execution_mode,
            "--stop-limit-offset-pct",
            str(max(0.0, float(stop_limit_offset_pct))),
            "--stop-limit-max-wait-events",
            str(max(1, int(stop_limit_max_wait_events))),
            "--runtime-guard-max-consecutive-losses",
            str(max(0, int(runtime_guard_max_consecutive_losses))),
            "--runtime-guard-max-drawdown-pct",
            str(max(0.0, float(runtime_guard_max_drawdown_pct))),
            "--quality-max-entries-per-day",
            str(max(0, int(quality_max_entries_per_day))),
            "--quality-entry-cutoff-hour",
            str(int(quality_entry_cutoff_hour)),
            "--quality-entry-cooldown-minutes",
            str(max(0, int(quality_entry_cooldown_minutes))),
            "--quality-min-side-prob",
            str(min(1.0, max(0.0, float(quality_min_side_prob)))),
            "--quality-min-prob-edge",
            str(min(1.0, max(0.0, float(quality_min_prob_edge)))),
            "--quality-skip-weekdays",
            quality_skip_weekdays,
            "--option-lot-size",
            str(max(1.0, float(option_lot_size))),
            "--output-jsonl",
            str(output_path),
            "--feature-trace-jsonl",
            str(feature_trace_path),
        ]
        if ce_threshold is not None:
            cmd.extend(["--ce-threshold", str(float(ce_threshold))])
        if pe_threshold is not None:
            cmd.extend(["--pe-threshold", str(float(pe_threshold))])
        if bool(trailing_enabled):
            cmd.append("--trailing-enabled")
        if not bool(trailing_lock_breakeven):
            cmd.append("--no-trailing-lock-breakeven")
        if bool(stagnation_enabled):
            cmd.append("--stagnation-enabled")

        _close_trading_log_handles(state)
        state["stdout_handle"] = open(stdout_path, "a", encoding="utf-8")
        state["stderr_handle"] = open(stderr_path, "a", encoding="utf-8")
        process = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=state["stdout_handle"],
            stderr=state["stderr_handle"],
        )

        state["process"] = process
        state["started_at"] = datetime.now(IST_TZ).isoformat()
        state["last_exit_code"] = None
        state["events_path"] = output_path
        state["stdout_path"] = stdout_path
        state["stderr_path"] = stderr_path
        state["config"] = dict(requested_identity)

        response_payload: Dict[str, Any] = {
            "status": "restarted" if restart_meta else "started",
            "instance": instance,
            "pid": process.pid,
            "started_at": state.get("started_at"),
            "events_path": str(output_path),
            "config": dict(state.get("config") or {}),
        }
        if restart_meta:
            response_payload["restart"] = restart_meta
        return response_payload


@app.post("/api/trading/stop")
async def stop_trading_runner(instance: Optional[str] = None):
    """Stop background paper capital runner."""
    instance_key = _normalize_trading_instance(instance)
    with _TRADING_LOCK:
        state = _refresh_trading_runner_state(instance_key)
        if state.get("process") is None:
            return {
                "status": "not_running",
                "instance": instance_key,
                "last_exit_code": state.get("last_exit_code"),
            }
        stop_meta = _stop_trading_process_locked(state, reason="manual_stop")
        return {
            "status": "stopped",
            "instance": instance_key,
            "last_exit_code": stop_meta.get("last_exit_code"),
        }


@app.get("/test", response_class=HTMLResponse)
async def test_page():
    """Test page for debugging"""
    test_page_path = Path(__file__).parent / "test_page.html"
    return HTMLResponse(test_page_path.read_text())

@app.get("/test/redis")
async def test_redis():
    """Test Redis connection"""
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        r.ping()
        instrument_pattern = DEFAULT_INSTRUMENT or "*"
        keys = r.keys(f"*{instrument_pattern}*")
        return {
            "connected": True,
            "host": REDIS_HOST,
            "port": REDIS_PORT,
            "total_keys": len(keys),
            "sample_keys": keys[:10] if keys else []
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}

@app.get("/test/ltp/{instrument}")
async def test_ltp(instrument: str):
    """Test LTP direct from Redis"""
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        ltp_raw = r.get(f"ltp:{instrument}")
        if ltp_raw:
            import json
            return json.loads(ltp_raw)
        return {"error": "No LTP data found"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/test/ohlc/{instrument}")
async def test_ohlc(instrument: str):
    """Test OHLC direct from Redis"""
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        import json
        
        # Try different keys
        keys_to_try = [
            f"live:ohlc_sorted:{instrument}:5min",
            f"ohlc_sorted:{instrument}:5min",
            f"live:ohlc_sorted:{instrument}:5m",
        ]
        
        for key in keys_to_try:
            entries = r.zrange(key, -5, -1)  # Last 5 bars
            if entries:
                bars = [json.loads(e) for e in entries]
                return {"key": key, "count": len(bars), "bars": bars}
        
        return {"error": "No OHLC data found", "tried_keys": keys_to_try}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "market-data-dashboard",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }

@app.get("/api/market-data/health")
async def market_data_health():
    """Get market data API health"""
    try:
        response = requests.get(f"{MARKET_DATA_API_URL}/health", timeout=5)
        return _normalize_timestamp_fields(response.json())
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        }

@app.get("/api/v1/system/mode")
async def get_system_mode():
    """Proxy system mode request to market data API"""
    try:
        response = requests.get(f"{MARKET_DATA_API_URL}/api/v1/system/mode", timeout=5)
        if response.status_code == 200:
            return _normalize_timestamp_fields(response.json())
        else:
            return {
                "mode": "unknown",
                "error": f"API returned status {response.status_code}",
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            }
    except Exception as e:
        return {
            "mode": "unknown",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        }


def _canonical_contract_timeframe(value: Optional[str]) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"1m", "1min", "minute"}:
        return "1m"
    if raw in {"5m", "5min"}:
        return "5m"
    if raw in {"15m", "15min"}:
        return "15m"
    return "1m"


def _timeframe_aliases_for_contract(tf: str) -> List[str]:
    return PUBLIC_TIMEFRAME_ALIASES.get(_canonical_contract_timeframe(tf), [_canonical_contract_timeframe(tf)])


def _mode_priority(mode_hint: Optional[str]) -> List[str]:
    modes = ["live", "historical", "paper"]
    mode = str(mode_hint or "").strip().lower()
    if mode in modes:
        return [mode] + [m for m in modes if m != mode]
    return modes


def _scan_keys_limited(
    r: redis.Redis,
    pattern: str,
    *,
    max_keys: int = 200,
    max_pages: int = 20,
) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    cursor = 0
    pages = 0
    while True:
        try:
            cursor, keys = r.scan(cursor=cursor, match=pattern, count=500)
        except Exception:
            break
        pages += 1
        for key in keys or []:
            sk = str(key)
            if sk in seen:
                continue
            seen.add(sk)
            out.append(sk)
            if len(out) >= max_keys:
                return out
        if cursor == 0 or pages >= max_pages:
            break
    return out


def _safe_json_loads(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


async def _load_runtime_instruments(max_instruments: int = 50) -> List[str]:
    default_instruments: List[str] = [DEFAULT_INSTRUMENT] if DEFAULT_INSTRUMENT else []
    instruments: List[str] = []
    try:
        response = requests.get(f"{MARKET_DATA_API_URL}/api/v1/market/instruments", timeout=2)
        if response.status_code == 200:
            payload = response.json()
            items = payload.get("instruments") if isinstance(payload, dict) else payload
            if isinstance(items, list):
                instruments = [str(x) for x in items if not _is_placeholder_instrument(x)]
    except Exception:
        pass

    discovered: List[str] = []
    if not instruments or instruments == default_instruments:
        discovered = await asyncio.to_thread(_discover_instruments_from_redis, max_instruments)
        if discovered:
            instruments = discovered + [inst for inst in instruments if inst not in discovered]

    if not instruments and default_instruments:
        instruments = default_instruments[:]

    deduped: List[str] = []
    seen: set[str] = set()
    for inst in instruments:
        val = str(inst).strip()
        if not val or val in seen or _is_placeholder_instrument(val):
            continue
        seen.add(val)
        deduped.append(val)
        if len(deduped) >= max_instruments:
            break
    return deduped


def _public_topic_schemas() -> Dict[str, Dict[str, Any]]:
    return {
        "mode": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "System Mode",
            "type": "object",
            "required": ["mode", "timestamp"],
            "properties": {
                "mode": {"type": "string", "enum": ["live", "historical", "paper", "unknown"]},
                "timestamp": {"type": "string", "format": "date-time"},
                "error": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "tick": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Tick Snapshot",
            "type": "object",
            "properties": {
                "instrument": {"type": "string"},
                "last_price": {"type": ["number", "null"]},
                "volume": {"type": ["number", "null"]},
                "timestamp": {"type": ["string", "null"], "format": "date-time"},
            },
            "additionalProperties": True,
        },
        "ohlc": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "OHLC Bars",
            "type": "array",
            "items": {
                "type": "object",
                "required": ["open", "high", "low", "close"],
                "properties": {
                    "start_at": {"type": ["string", "null"], "format": "date-time"},
                    "open": {"type": "number"},
                    "high": {"type": "number"},
                    "low": {"type": "number"},
                    "close": {"type": "number"},
                    "volume": {"type": ["number", "null"]},
                    "oi": {"type": ["number", "null"]},
                },
                "additionalProperties": True,
            },
        },
        "indicators": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Indicators Payload",
            "type": "object",
            "required": ["instrument", "timeframe", "status"],
            "properties": {
                "instrument": {"type": "string"},
                "timeframe": {"type": "string"},
                "status": {"type": "string"},
                "timestamp": {"type": ["string", "null"], "format": "date-time"},
                "bars_available": {"type": ["integer", "number"]},
                "indicators": {"type": "object"},
            },
            "additionalProperties": True,
        },
        "depth": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Market Depth",
            "type": "object",
            "required": ["status", "source", "timestamp", "instrument", "buy", "sell"],
            "properties": {
                "status": {"type": "string"},
                "source": {"type": "string"},
                "timestamp": {"type": "string", "format": "date-time"},
                "instrument": {"type": "string"},
                "underlying_price": {"type": ["number", "null"]},
                "buy": {"type": "array"},
                "sell": {"type": "array"},
            },
            "additionalProperties": True,
        },
        "options": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Options Chain",
            "type": "object",
            "required": ["status", "source", "timestamp", "instrument", "strikes"],
            "properties": {
                "status": {"type": "string"},
                "source": {"type": "string"},
                "timestamp": {"type": "string", "format": "date-time"},
                "instrument": {"type": "string"},
                "expiry": {"type": ["string", "null"]},
                "underlying_price": {"type": ["number", "null"]},
                "futures_price": {"type": ["number", "null"]},
                "pcr": {"type": ["number", "null"]},
                "max_pain": {"type": ["number", "integer", "null"]},
                "strikes": {"type": "array"},
            },
            "additionalProperties": True,
        },
    }


async def _build_runtime_catalog(instrument: Optional[str] = None) -> Dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    instruments = await _load_runtime_instruments(max_instruments=50)
    selected_instrument = str(instrument or "").strip() or (instruments[0] if instruments else (DEFAULT_INSTRUMENT or ""))
    mode_hint = _get_current_mode_hint(timeout_seconds=1.0)
    mode_candidates = _mode_priority(mode_hint)

    dashboard_port = os.getenv("DASHBOARD_PORT") or os.getenv("MARKET_DATA_DASHBOARD_PORT") or "8002"
    dashboard_base = f"http://127.0.0.1:{dashboard_port}"

    catalog: Dict[str, Any] = {
        "status": "ok",
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "timestamp": now_iso,
        "mode": mode_hint or "unknown",
        "mode_candidates": mode_candidates,
        "instrument": selected_instrument or None,
        "instruments": instruments,
        "timeframes": list(PUBLIC_TIMEFRAMES),
        "redis": {
            "host": REDIS_HOST,
            "port": REDIS_PORT,
            "db": 0,
            "mode_prefix": mode_hint or "unknown",
            "keys": {},
        },
        "apis": {
            "mode_info": f"{MARKET_DATA_API_URL}/api/v1/system/mode",
            "tick": f"{MARKET_DATA_API_URL}/api/v1/market/tick/{selected_instrument}" if selected_instrument else None,
            "ohlc": f"{dashboard_base}/api/market-data/ohlc/{selected_instrument}?timeframe=1m&limit=20" if selected_instrument else None,
            "indicators": f"{dashboard_base}/api/market-data/indicators/{selected_instrument}?timeframe=1m" if selected_instrument else None,
            "options": f"{dashboard_base}/api/market-data/options/{selected_instrument}" if selected_instrument else None,
            "depth": f"{dashboard_base}/api/market-data/depth/{selected_instrument}" if selected_instrument else None,
        },
        "ws_topics": {
            "ohlc_all_tf": f"/topic/market/ohlc/{selected_instrument}" if selected_instrument else None,
            "ohlc_tf": f"/topic/market/ohlc/{selected_instrument}/1m" if selected_instrument else None,
            "indicators": f"/topic/indicators/{selected_instrument}" if selected_instrument else None,
            "ticks": f"/topic/market/tick/{selected_instrument}" if selected_instrument else None,
        },
        "availability": {},
    }

    if not selected_instrument:
        catalog["status"] = "no_instrument"
        return _normalize_timestamp_fields(catalog)

    try:
        r = _redis_sync_client()
        r.ping()
    except Exception as e:
        catalog["status"] = "degraded"
        catalog["redis"]["error"] = str(e)
        catalog["availability"] = {
            "tick": False,
            "price": False,
            "volume": False,
            "ohlc": False,
            "indicators": False,
            "depth": False,
            "options": False,
        }
        return _normalize_timestamp_fields(catalog)

    def pick_string_value(suffixes: List[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        for m in mode_candidates:
            for suffix in suffixes:
                key = f"{m}:{suffix}"
                try:
                    value = r.get(key)
                    if value is not None:
                        return key, value, m
                except Exception:
                    continue
        return None, None, None

    def pick_zset_key(suffixes: List[str]) -> Tuple[Optional[str], int, Optional[str]]:
        for m in mode_candidates:
            for suffix in suffixes:
                key = f"{m}:{suffix}"
                try:
                    count = int(r.zcard(key) or 0)
                except Exception:
                    continue
                if count > 0:
                    return key, count, m
        return None, 0, None

    # Tick / Price / Volume snapshots
    tick_key, tick_raw, tick_mode = pick_string_value([
        f"websocket:tick:{selected_instrument}:latest",
        f"tick:{selected_instrument}:latest",
        f"tick:{selected_instrument}",
    ])
    price_key, price_raw, price_mode = pick_string_value([
        f"price:{selected_instrument}:latest",
        f"ltp:{selected_instrument}",
    ])
    volume_key, volume_raw, volume_mode = pick_string_value([
        f"volume:{selected_instrument}:latest",
    ])

    tick_obj = _safe_json_loads(tick_raw) if tick_raw else None
    price_obj = _safe_json_loads(price_raw) if price_raw else None
    tick_timestamp = None
    if isinstance(tick_obj, dict):
        tick_timestamp = _normalize_timestamp_string(tick_obj.get("timestamp") or tick_obj.get("exchange_timestamp"))

    catalog["redis"]["keys"]["tick_latest"] = {
        "key": tick_key,
        "mode": tick_mode,
        "present": bool(tick_key),
        "timestamp": tick_timestamp,
    }
    catalog["redis"]["keys"]["price_latest"] = {
        "key": price_key,
        "mode": price_mode,
        "present": bool(price_key),
        "value": price_obj if isinstance(price_obj, dict) else price_raw,
    }
    catalog["redis"]["keys"]["volume_latest"] = {
        "key": volume_key,
        "mode": volume_mode,
        "present": bool(volume_key),
        "value": volume_raw,
    }

    # OHLC sorted sets
    ohlc_info: Dict[str, Any] = {}
    for tf in PUBLIC_TIMEFRAMES:
        aliases = _timeframe_aliases_for_contract(tf)
        suffixes = [f"ohlc_sorted:{selected_instrument}:{alias}" for alias in aliases]
        ohlc_key, ohlc_count, ohlc_mode = pick_zset_key(suffixes)
        latest_ts = None
        latest_close = None
        if ohlc_key and ohlc_count > 0:
            try:
                row = r.zrange(ohlc_key, -1, -1)
                if row:
                    obj = _safe_json_loads(row[0])
                    if isinstance(obj, dict):
                        latest_ts = _normalize_timestamp_string(obj.get("start_at") or obj.get("timestamp"))
                        latest_close = obj.get("close")
            except Exception:
                pass
        ohlc_info[tf] = {
            "key": ohlc_key,
            "mode": ohlc_mode,
            "aliases_checked": aliases,
            "present": bool(ohlc_key),
            "count": int(ohlc_count),
            "latest_timestamp": latest_ts,
            "latest_close": latest_close,
        }
    catalog["redis"]["keys"]["ohlc_sorted"] = ohlc_info

    # Indicators keys by timeframe
    indicators_info: Dict[str, Any] = {}
    for tf in PUBLIC_TIMEFRAMES:
        aliases = _timeframe_aliases_for_contract(tf)
        found_mode: Optional[str] = None
        sample_keys: List[str] = []
        for m in mode_candidates:
            tf_keys: List[str] = []
            for alias in aliases:
                tf_keys.extend(
                    _scan_keys_limited(
                        r,
                        f"{m}:indicators:{selected_instrument}:{alias}:*",
                        max_keys=120,
                        max_pages=20,
                    )
                )
            if tf_keys:
                found_mode = m
                sample_keys = sorted(list(dict.fromkeys(tf_keys)))
                break
        indicators_info[tf] = {
            "pattern": f"{{mode}}:indicators:{selected_instrument}:{tf}:*",
            "aliases_checked": aliases,
            "mode": found_mode,
            "present": bool(sample_keys),
            "count": len(sample_keys),
            "sample_keys": sample_keys[:5],
        }
    catalog["redis"]["keys"]["indicators"] = indicators_info

    # Depth keys
    depth_latest_key, depth_latest_raw, depth_mode = pick_string_value([
        f"depth:{selected_instrument}:latest",
    ])
    depth_buy_key, depth_buy_raw, depth_buy_mode = pick_string_value([
        f"depth:{selected_instrument}:buy",
    ])
    depth_sell_key, depth_sell_raw, depth_sell_mode = pick_string_value([
        f"depth:{selected_instrument}:sell",
    ])
    depth_ts_key, depth_ts_raw, depth_ts_mode = pick_string_value([
        f"depth:{selected_instrument}:timestamp",
    ])
    catalog["redis"]["keys"]["depth"] = {
        "mode": depth_mode or depth_buy_mode or depth_sell_mode or depth_ts_mode,
        "latest_key": depth_latest_key,
        "buy_key": depth_buy_key,
        "sell_key": depth_sell_key,
        "timestamp_key": depth_ts_key,
        "present": bool(depth_latest_key or (depth_buy_key and depth_sell_key)),
        "timestamp": _normalize_timestamp_string(depth_ts_raw),
        "top_buy_levels": len(_safe_json_loads(depth_buy_raw) or []) if depth_buy_raw else 0,
        "top_sell_levels": len(_safe_json_loads(depth_sell_raw) or []) if depth_sell_raw else 0,
    }

    # Options chain key (with and without expiry component)
    options_key: Optional[str] = None
    options_mode: Optional[str] = None
    options_payload: Optional[Dict[str, Any]] = None
    for m in mode_candidates:
        direct_key = f"{m}:options:{selected_instrument}:chain"
        try:
            direct_val = r.get(direct_key)
            if direct_val:
                options_key = direct_key
                options_mode = m
                parsed = _safe_json_loads(direct_val)
                options_payload = parsed if isinstance(parsed, dict) else None
                break
        except Exception:
            pass
        expiry_keys = _scan_keys_limited(
            r,
            f"{m}:options:{selected_instrument}:*:chain",
            max_keys=1,
            max_pages=8,
        )
        if expiry_keys:
            options_key = expiry_keys[0]
            options_mode = m
            try:
                raw = r.get(options_key)
                parsed = _safe_json_loads(raw)
                options_payload = parsed if isinstance(parsed, dict) else None
            except Exception:
                options_payload = None
            break
    catalog["redis"]["keys"]["options_chain"] = {
        "key": options_key,
        "mode": options_mode,
        "present": bool(options_key),
        "expiry": (options_payload or {}).get("expiry") if isinstance(options_payload, dict) else None,
        "strikes_count": len((options_payload or {}).get("strikes") or []) if isinstance(options_payload, dict) else 0,
    }

    # API-level probes to support scenarios where data is served via API fallback
    # even when canonical Redis keys are not populated yet.
    api_probes: Dict[str, Any] = {}
    probe_targets = {
        "tick": f"{MARKET_DATA_API_URL}/api/v1/market/tick/{selected_instrument}",
        "ohlc": f"{MARKET_DATA_API_URL}/api/v1/market/ohlc/{selected_instrument}?timeframe=1m&limit=2",
        "indicators": f"{MARKET_DATA_API_URL}/api/v1/technical/indicators/{selected_instrument}?timeframe=minute",
        "depth": f"{MARKET_DATA_API_URL}/api/v1/market/depth/{selected_instrument}",
        "options": f"{MARKET_DATA_API_URL}/api/v1/options/chain/{selected_instrument}",
    }
    for probe_name, probe_url in probe_targets.items():
        probe_status = {"ok": False, "http_status": None}
        try:
            resp = requests.get(probe_url, timeout=(1.5, 3))
            probe_status["http_status"] = resp.status_code
            if resp.status_code == 200:
                payload = resp.json()
                if probe_name == "ohlc":
                    probe_status["ok"] = isinstance(payload, list) and len(payload) > 0
                elif probe_name == "indicators":
                    if isinstance(payload, dict):
                        indicators_obj = payload.get("indicators")
                        probe_status["ok"] = bool(
                            payload.get("status") in {"ok", "stale"}
                            or isinstance(indicators_obj, dict)
                            or ("data" in payload and isinstance(payload.get("data"), dict))
                        )
                elif probe_name == "depth":
                    if isinstance(payload, dict):
                        probe_status["ok"] = bool(
                            payload.get("status") in {"ok", "stale"}
                            or isinstance(payload.get("buy"), list)
                            or isinstance(payload.get("sell"), list)
                        )
                elif probe_name == "options":
                    if isinstance(payload, dict):
                        probe_status["ok"] = bool(
                            payload.get("status") in {"ok", "stale", "synthetic"}
                            or isinstance(payload.get("strikes"), list)
                        )
                else:
                    probe_status["ok"] = True
        except Exception as e:
            probe_status["error"] = str(e)
        api_probes[probe_name] = probe_status
    catalog["api_probes"] = api_probes

    redis_tick = bool(catalog["redis"]["keys"]["tick_latest"].get("present"))
    redis_price = bool(catalog["redis"]["keys"]["price_latest"].get("present"))
    redis_volume = bool(catalog["redis"]["keys"]["volume_latest"].get("present"))
    redis_ohlc = any(bool(v.get("present")) for v in ohlc_info.values())
    redis_indicators = any(bool(v.get("present")) for v in indicators_info.values())
    redis_depth = bool(catalog["redis"]["keys"]["depth"].get("present"))
    redis_options = bool(catalog["redis"]["keys"]["options_chain"].get("present"))

    catalog["availability"] = {
        "tick": redis_tick or bool(api_probes.get("tick", {}).get("ok")),
        "price": redis_price,
        "volume": redis_volume,
        "ohlc": redis_ohlc or bool(api_probes.get("ohlc", {}).get("ok")),
        "indicators": redis_indicators or bool(api_probes.get("indicators", {}).get("ok")),
        "depth": redis_depth or bool(api_probes.get("depth", {}).get("ok")),
        "options": redis_options or bool(api_probes.get("options", {}).get("ok")),
    }
    catalog["availability_detail"] = {
        "redis": {
            "tick": redis_tick,
            "price": redis_price,
            "volume": redis_volume,
            "ohlc": redis_ohlc,
            "indicators": redis_indicators,
            "depth": redis_depth,
            "options": redis_options,
        },
        "api": {k: bool(v.get("ok")) for k, v in api_probes.items()},
    }

    return _normalize_timestamp_fields(catalog)


@app.get("/api/schema")
async def get_public_schema_index():
    """Versioned schema index for external consumers."""
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    schemas = _public_topic_schemas()
    topics = [
        {
            "topic": topic,
            "version": PUBLIC_SCHEMA_VERSION,
            "schema_url": f"/api/schema/{topic}",
            "example_url": f"/api/examples/{topic}",
        }
        for topic in PUBLIC_TOPICS
        if topic in schemas
    ]
    return _normalize_timestamp_fields(
        {
            "status": "ok",
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "timestamp": now_iso,
            "topics": topics,
        }
    )


@app.get("/api/schema/{topic}")
async def get_public_topic_schema(topic: str):
    """Return JSON Schema for a single topic."""
    topic_key = str(topic or "").strip().lower()
    schemas = _public_topic_schemas()
    if topic_key not in schemas:
        raise HTTPException(status_code=404, detail=f"Unknown topic '{topic_key}'. Supported: {', '.join(PUBLIC_TOPICS)}")
    return _normalize_timestamp_fields(
        {
            "status": "ok",
            "topic": topic_key,
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "schema": schemas[topic_key],
        }
    )


@app.get("/api/capabilities")
async def get_public_capabilities(instrument: str = None):
    """Dynamic runtime capabilities for current mode/instrument set."""
    catalog = await _build_runtime_catalog(instrument=instrument)
    selected_instrument = catalog.get("instrument")
    return _normalize_timestamp_fields(
        {
            "status": catalog.get("status", "ok"),
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": catalog.get("mode"),
            "instruments": catalog.get("instruments", []),
            "default_instrument": selected_instrument,
            "timeframes": list(PUBLIC_TIMEFRAMES),
            "topics": list(PUBLIC_TOPICS),
            "availability": catalog.get("availability", {}),
            "apis": catalog.get("apis", {}),
            "ws_topics": catalog.get("ws_topics", {}),
            "schema_index": "/api/schema",
        }
    )


@app.get("/api/catalog")
async def get_public_catalog(instrument: str = None):
    """Dynamic key/API catalog resolved at runtime for one instrument."""
    return await _build_runtime_catalog(instrument=instrument)


@app.get("/api/examples/{topic}")
async def get_public_topic_example(topic: str, instrument: str = None, timeframe: str = "1m"):
    """Return a current runtime sample payload for a topic."""
    topic_key = str(topic or "").strip().lower()
    if topic_key not in PUBLIC_TOPICS:
        raise HTTPException(status_code=404, detail=f"Unknown topic '{topic_key}'. Supported: {', '.join(PUBLIC_TOPICS)}")

    instruments = await _load_runtime_instruments(max_instruments=20)
    selected_instrument = str(instrument or "").strip() or (instruments[0] if instruments else DEFAULT_INSTRUMENT)
    tf = _canonical_contract_timeframe(timeframe)
    tf_for_endpoint = tf if tf != "1m" else "1min"

    sample: Any = None
    if topic_key != "mode" and not selected_instrument:
        sample = {"status": "no_data", "message": "No instrument available"}
    elif topic_key == "mode":
        sample = await get_system_mode()
    elif topic_key == "tick":
        try:
            resp = requests.get(
                f"{MARKET_DATA_API_URL}/api/v1/market/tick/{selected_instrument}",
                timeout=3,
            )
            if resp.status_code == 200:
                sample = _normalize_timestamp_fields(resp.json())
            else:
                sample = {"status": "no_data", "error": f"Upstream tick API returned {resp.status_code}"}
        except Exception as e:
            sample = {"status": "error", "error": str(e)}
    elif topic_key == "ohlc":
        sample = await get_ohlc_data(
            instrument=selected_instrument,
            timeframe=tf_for_endpoint,
            limit=3,
            order="desc",
        )
    elif topic_key == "indicators":
        sample = await get_technical_indicators(
            instrument=selected_instrument,
            timeframe=tf_for_endpoint,
        )
    elif topic_key == "depth":
        sample = await get_market_depth(selected_instrument)
    elif topic_key == "options":
        sample = await get_options_chain(selected_instrument)

    return _normalize_timestamp_fields(
        {
            "status": "ok",
            "topic": topic_key,
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": _get_current_mode_hint(timeout_seconds=1.0) or "unknown",
            "instrument": selected_instrument,
            "timeframe": tf,
            "sample": sample,
        }
    )


@app.get("/api/market-data/status")
async def market_data_status():
    """Get comprehensive market data status"""
    status = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "market_data_api": {"status": "unknown"},
        "redis": {"status": "unknown"},
        "instruments": {},
        "data_validation": {}
    }

    # Check market data API health
    try:
        health_response = requests.get(f"{MARKET_DATA_API_URL}/health", timeout=5)
        status["market_data_api"] = health_response.json()
    except Exception as e:
        status["market_data_api"] = {
            "status": "unreachable",
            "error": str(e)
        }

    # Prefer Redis for instrument/data availability (fast, avoids API key-prefix mismatches).
    default_instruments: List[str] = [DEFAULT_INSTRUMENT] if DEFAULT_INSTRUMENT else []
    instruments: List[str] = default_instruments[:]
    try:
        resp = requests.get(f"{MARKET_DATA_API_URL}/api/v1/market/instruments", timeout=2)
        if resp.status_code == 200:
            api_instruments = resp.json()
            if isinstance(api_instruments, dict) and "instruments" in api_instruments:
                api_instruments = api_instruments["instruments"]
            if isinstance(api_instruments, list) and api_instruments:
                instruments = [str(x) for x in api_instruments if not _is_placeholder_instrument(x)]
    except Exception:
        pass

    try:
        r = _redis_sync_client()
        r.ping()
        status["redis"] = {"status": "healthy", "host": REDIS_HOST, "port": REDIS_PORT}
    except Exception as e:
        status["redis"] = {"status": "unhealthy", "error": str(e), "host": REDIS_HOST, "port": REDIS_PORT}
        r = None

    # If API instruments endpoint is missing/unhelpful, auto-discover instruments from Redis.
    if r is not None and (not instruments or instruments == default_instruments):
        try:
            discovered = await asyncio.to_thread(_discover_instruments_from_redis, 25)
            if discovered:
                instruments = discovered
        except Exception:
            pass

    api_mode = str(status.get("market_data_api", {}).get("mode") or "").strip().lower()
    if api_mode not in {"live", "historical", "paper"}:
        api_mode = None

    for instrument in instruments:
        try:
            if not r:
                status["instruments"][instrument] = {"status": "unreachable", "error": "Redis unavailable"}
                continue

            # Prefer keys from current execution mode, then fall back to any mode.
            # This avoids showing historical namespace as green/available during live runs.
            best_key = None
            best_count = 0
            best_mode_key = None
            best_mode_count = 0

            for key in _ohlc_sorted_keys_to_try(
                instrument,
                "1min",
                preferred_mode=api_mode,
                strict_mode=bool(api_mode),
            ):
                try:
                    c = r.zcard(key)
                    if not c:
                        continue
                    key_mode = _extract_key_mode(key)
                    if api_mode and key_mode == api_mode:
                        if c > best_mode_count:
                            best_mode_key = key
                            best_mode_count = c
                    elif c > best_count:
                        best_key = key
                        best_count = c
                except Exception:
                    continue

            if best_mode_key:
                best_key = best_mode_key
                best_count = best_mode_count

            if not best_key or best_count == 0:
                status["instruments"][instrument] = {
                    "status": "no_data",
                    "data_points": 0,
                    "first_timestamp": None,
                    "latest_timestamp": None,
                    "latest_price": None,
                }
                continue

            first_row = r.zrange(best_key, 0, 0)
            last_row = r.zrange(best_key, -1, -1)
            first_bar = json.loads(first_row[0]) if first_row else {}
            last_bar = json.loads(last_row[0]) if last_row else {}

            data_mode = _extract_key_mode(best_key)
            mode_mismatch = bool(api_mode and data_mode and data_mode != api_mode)

            status["instruments"][instrument] = {
                "status": "mode_mismatch" if mode_mismatch else "available",
                "data_points": int(best_count),
                "first_timestamp": _normalize_timestamp_string(_extract_bar_timestamp(first_bar)),
                "latest_timestamp": _normalize_timestamp_string(_extract_bar_timestamp(last_bar)),
                "latest_price": last_bar.get("close") or last_bar.get("last_price"),
                "redis_key": best_key,
                "data_mode": data_mode,
                "expected_mode": api_mode,
                "mode_mismatch": mode_mismatch,
            }
        except Exception as e:
            status["instruments"][instrument] = {"status": "error", "error": str(e)}

    # Data validation checks
    status["data_validation"] = validate_data_availability(status)

    return _normalize_timestamp_fields(status)

def validate_data_availability(status: Dict[str, Any]) -> Dict[str, Any]:
    """Validate data availability and freshness"""
    validation = {
        "overall_status": "unknown",
        "checks": {}
    }

    # Check if API is healthy
    api_healthy = status.get("market_data_api", {}).get("status") == "healthy"
    validation["checks"]["api_health"] = api_healthy

    # Check data availability
    instruments_available = 0
    instruments_with_data = 0
    for instrument, info in status.get("instruments", {}).items():
        if info.get("status") == "available":
            instruments_available += 1
            if info.get("data_points", 0) > 0:
                instruments_with_data += 1

    validation["checks"]["instruments_available"] = instruments_available
    validation["checks"]["instruments_with_data"] = instruments_with_data
    validation["checks"]["total_instruments"] = len(status.get("instruments", {}))

    # Overall status
    # IMPORTANT: In historical mode, Redis-backed data can be valid even if the
    # upstream Market Data API health check is temporarily red.
    if instruments_with_data == 0:
        validation["overall_status"] = "critical" if not api_healthy else "warning"
    elif not api_healthy:
        validation["overall_status"] = "degraded"
    elif instruments_with_data >= 2:
        validation["overall_status"] = "healthy"
    else:
        validation["overall_status"] = "partial"

    return validation

@app.get("/api/market-data/ohlc/{instrument}")
async def get_ohlc_data(
    instrument: str,
    timeframe: str = "1min",
    limit: int = 100,
    order: str = "asc",
):
    """Get OHLC data for an instrument with specified timeframe, filtered by virtual time in historical mode."""
    try:
        mode_hint = _get_current_mode_hint(timeout_seconds=1.0)

        # Fast path: read directly from Redis (handles live:/historical:/paper: prefixes).
        redis_bars, redis_key = await asyncio.to_thread(
            _read_ohlc_from_redis, instrument, timeframe, limit, order, mode_hint, bool(mode_hint)
        )
        if redis_bars:
            filtered = filter_data_by_virtual_time(redis_bars, "start_at")
            canonical_filtered = _merge_ohlc_bars_by_timeframe(filtered, timeframe)

            # Higher-timeframe sorted sets may exist without OI even when 1-min has OI.
            # In that case, rebuild from 1-min so chart OI series remains available.
            if timeframe != "1min" and canonical_filtered and not _has_any_oi(canonical_filtered):
                base_limit = _determine_base_limit(timeframe, limit)
                base_bars, _ = await asyncio.to_thread(
                    _read_ohlc_from_redis, instrument, "1min", base_limit, "asc", mode_hint, bool(mode_hint)
                )
                if base_bars:
                    base_filtered = filter_data_by_virtual_time(base_bars, "start_at")
                    aggregated = aggregate_ohlc(base_filtered, timeframe)
                    out = aggregated[-limit:] if limit and len(aggregated) > limit else aggregated
                    return _normalize_timestamp_fields(out)

            out = canonical_filtered[-limit:] if limit and len(canonical_filtered) > limit else canonical_filtered
            return _normalize_timestamp_fields(out)

        # If requested TF isn't present, aggregate from 1-min bars from Redis.
        if timeframe != "1min":
            base_limit = _determine_base_limit(timeframe, limit)
            base_bars, _ = await asyncio.to_thread(
                _read_ohlc_from_redis, instrument, "1min", base_limit, "asc", mode_hint, bool(mode_hint)
            )
            if base_bars:
                base_filtered = filter_data_by_virtual_time(base_bars, "start_at")
                aggregated = aggregate_ohlc(base_filtered, timeframe)
                out = aggregated[-limit:] if limit and len(aggregated) > limit else aggregated
                return _normalize_timestamp_fields(out)

        # Fallback to API (useful if Redis is empty or running remotely)
        response = requests.get(
            f"{MARKET_DATA_API_URL}/api/v1/market/ohlc/{instrument}?timeframe={timeframe}&limit={limit}&order={order}",
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            filtered_data = filter_data_by_virtual_time(data, "start_at")
            canonical_filtered = _merge_ohlc_bars_by_timeframe(filtered_data, timeframe)
            out = canonical_filtered[-limit:] if limit and len(canonical_filtered) > limit else canonical_filtered
            return _normalize_timestamp_fields(out)

        if response.status_code == 404 and timeframe != "1min":
            base_limit = _determine_base_limit(timeframe, limit)
            fallback = requests.get(
                f"{MARKET_DATA_API_URL}/api/v1/market/ohlc/{instrument}?timeframe=1min&limit={base_limit}&order=asc",
                timeout=10,
            )
            if fallback.status_code != 200:
                raise HTTPException(status_code=fallback.status_code, detail="Failed to fetch OHLC data for aggregation")

            base_data = filter_data_by_virtual_time(fallback.json(), "start_at")
            filtered_data = aggregate_ohlc(base_data, timeframe)
            out = filtered_data[-limit:] if limit and len(filtered_data) > limit else filtered_data
            return _normalize_timestamp_fields(out)

        raise HTTPException(status_code=response.status_code, detail=response.text or "Failed to fetch OHLC data")

    except HTTPException as http_exc:
        logger.warning(
            "OHLC fetch failed for %s %s (limit=%s): %s",
            instrument,
            timeframe,
            limit,
            http_exc.detail,
        )
        raise
    except Exception as e:
        logger.exception("Unexpected error fetching OHLC for %s %s", instrument, timeframe)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/market-data/charts/{instrument}")
async def get_chart_data(
    instrument: str,
    timeframe: str = "1min",
    limit: int = 200,
):
    """Return chart-ready payload (price + indicators) for thin frontend rendering."""
    try:
        tf = str(timeframe or "1min")
        limit_by_tf = {
            "1min": 1500,
            "5min": 500,
            "15min": 200,
            "1h": 150,
            "4h": 150,
            "1d": 150,
        }
        req_limit = max(1, int(limit or 0))
        req_limit = limit_by_tf.get(tf, req_limit)
        indicators_bars_needed = 120
        combined_limit = max(req_limit, indicators_bars_needed)

        ohlc_data = await get_ohlc_data(
            instrument=instrument,
            timeframe=tf,
            limit=combined_limit,
            order="asc",
        )
        if not isinstance(ohlc_data, list):
            ohlc_data = []

        payload = _build_chart_payload_from_ohlc(
            instrument=instrument,
            timeframe=tf,
            ohlc_data=ohlc_data,
            req_limit=req_limit,
            indicators_bars_needed=indicators_bars_needed,
        )
        payload["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload["status"] = "ok"
        return _normalize_timestamp_fields(payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error building chart data for %s %s", instrument, timeframe)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/market-data/indicators/{instrument}")
async def get_technical_indicators(instrument: str, timeframe: str = "1min"):
    """Get technical indicators for an instrument"""
    cache_key = f"{instrument}:{timeframe}"
    try:
        # Normalize timeframe for API call
        tf = timeframe
        if tf == "1min":
            tf = "minute"  # API expects "minute" for 1min

        response = requests.get(
            f"{MARKET_DATA_API_URL}/api/v1/technical/indicators/{instrument}?timeframe={tf}",
            timeout=(1.5, 4)
        )
        if response.status_code == 200:
            payload = _normalize_timestamp_fields(response.json())
            if isinstance(payload, dict):
                payload.setdefault("instrument", instrument)
                payload.setdefault("timeframe", timeframe)
                payload.setdefault("status", "ok")
                indicators_payload = payload.get("indicators") if isinstance(payload.get("indicators"), dict) else {}
                payload.setdefault(
                    "indicator_timestamp",
                    payload.get("timestamp")
                    or indicators_payload.get("indicator_timestamp")
                    or indicators_payload.get("timestamp")
                )
                payload.setdefault(
                    "indicator_source",
                    indicators_payload.get("source") or "market_data_api"
                )
                payload.setdefault(
                    "indicator_stream",
                    payload.get("stream") or indicators_payload.get("indicator_stream") or "Y2"
                )
                payload.setdefault(
                    "indicator_update_type",
                    indicators_payload.get("indicator_update_type") or indicators_payload.get("update_type") or "batch_recalculate"
                )
                payload.setdefault("bars_available", indicators_payload.get("bars_available", 0))
                payload.setdefault(
                    "warmup_requirements",
                    payload.get("warmup_requirements") or indicators_payload.get("warmup_requirements") or {}
                )
                _LAST_GOOD_INDICATORS[cache_key] = payload
            return payload

        # Upstream returned non-200: serve stale cache if present.
        cached = _LAST_GOOD_INDICATORS.get(cache_key)
        if cached:
            out = dict(cached)
            out["status"] = "stale"
            out["warning"] = f"Upstream indicators API returned {response.status_code}"
            out["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            out.setdefault("indicator_timestamp", out.get("timestamp"))
            out.setdefault("indicator_source", "cache")
            out.setdefault("indicator_stream", out.get("indicator_stream") or "Y2")
            out.setdefault("indicator_update_type", out.get("indicator_update_type") or "cache")
            out.setdefault("bars_available", out.get("bars_available", 0))
            out.setdefault("warmup_requirements", out.get("warmup_requirements", {}))
            return _normalize_timestamp_fields(out)

        return {
            "instrument": instrument,
            "timeframe": timeframe,
            "indicators": {},
            "status": "no_data",
            "error": f"Upstream indicators API returned {response.status_code}",
            "indicator_timestamp": None,
            "indicator_source": "no_data",
            "indicator_stream": "Y2",
            "indicator_update_type": "no_data",
            "bars_available": 0,
            "warmup_requirements": {},
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        }
    except Exception as e:
        cached = _LAST_GOOD_INDICATORS.get(cache_key)
        if cached:
            out = dict(cached)
            out["status"] = "stale"
            out["warning"] = f"Using cached indicators due to upstream error: {e}"
            out["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            out.setdefault("indicator_timestamp", out.get("timestamp"))
            out.setdefault("indicator_source", "cache")
            out.setdefault("indicator_stream", out.get("indicator_stream") or "Y2")
            out.setdefault("indicator_update_type", out.get("indicator_update_type") or "cache")
            out.setdefault("bars_available", out.get("bars_available", 0))
            out.setdefault("warmup_requirements", out.get("warmup_requirements", {}))
            return _normalize_timestamp_fields(out)

        return {
            "instrument": instrument,
            "timeframe": timeframe,
            "indicators": {},
            "status": "error",
            "error": str(e),
            "indicator_timestamp": None,
            "indicator_source": "error",
            "indicator_stream": "Y2",
            "indicator_update_type": "error",
            "bars_available": 0,
            "warmup_requirements": {},
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        }

@app.get("/api/market-data/instruments")
async def get_available_instruments():
    """Get list of available instruments"""
    try:
        response = requests.get(f"{MARKET_DATA_API_URL}/api/v1/market/instruments", timeout=5)
        if response.status_code == 200:
            payload = response.json()
            instruments = payload.get("instruments") if isinstance(payload, dict) else []
            if isinstance(instruments, list):
                instruments = [str(x) for x in instruments if not _is_placeholder_instrument(x)]
            return _normalize_timestamp_fields(
                {
                    "instruments": instruments,
                    "count": len(instruments),
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            )
        else:
            # fallback to Redis discovery
            discovered = await asyncio.to_thread(_discover_instruments_from_redis, 50)
            return {"instruments": discovered or ([DEFAULT_INSTRUMENT] if DEFAULT_INSTRUMENT else [])}
    except Exception as e:
        discovered = await asyncio.to_thread(_discover_instruments_from_redis, 50)
        return {"instruments": discovered or ([DEFAULT_INSTRUMENT] if DEFAULT_INSTRUMENT else [])}

@app.get("/api/market-data/depth/{instrument}")
async def get_market_depth(instrument: str):
    """Get market depth (order book) for an instrument"""
    cache_key = instrument
    upstream_error: Optional[str] = None
    mode_hint = _get_current_mode_hint()
    try:
        # First try the Market Data API
        try:
            response = requests.get(
                f"{MARKET_DATA_API_URL}/api/v1/market/depth/{instrument}",
                timeout=(1.5, 3)
            )
            if response.status_code == 200:
                payload = _normalize_depth_contract(
                    instrument,
                    _normalize_timestamp_fields(response.json()),
                    mode_hint=mode_hint,
                    default_status="ok",
                )
                if isinstance(payload, dict):
                    payload.setdefault("status", "ok")
                    _LAST_GOOD_DEPTH[cache_key] = payload
                return payload
            upstream_error = f"Upstream depth API returned {response.status_code}"
        except Exception as api_err:
            upstream_error = str(api_err)
        
        # If API doesn't have endpoint, read directly from Redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        
        buy_data = r.get(get_redis_key(f"depth:{instrument}:buy"))
        sell_data = r.get(get_redis_key(f"depth:{instrument}:sell"))
        timestamp = r.get(get_redis_key(f"depth:{instrument}:timestamp"))
        
        if not buy_data or not sell_data:
            cached = _LAST_GOOD_DEPTH.get(cache_key)
            if cached:
                out = dict(cached)
                out["status"] = "stale"
                out["warning"] = upstream_error or "No fresh depth data available"
                out["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                return _normalize_depth_contract(
                    instrument,
                    out,
                    mode_hint=mode_hint,
                    default_status="stale",
                )
            return _normalize_depth_contract(instrument, {
                "instrument": instrument,
                "buy": [],
                "sell": [],
                "timestamp": _normalize_timestamp_string(timestamp) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "status": "no_data",
                "warning": upstream_error,
            }, mode_hint=mode_hint, default_status="no_data")
        
        buy_levels = json.loads(buy_data)
        sell_levels = json.loads(sell_data)
        
        out = {
            "instrument": instrument,
            "buy": buy_levels[:5],  # Top 5 bids
            "sell": sell_levels[:5],  # Top 5 asks
            "timestamp": _normalize_timestamp_string(timestamp) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "ok"
        }
        out = _normalize_depth_contract(
            instrument,
            out,
            mode_hint=mode_hint,
            default_status="ok",
        )
        _LAST_GOOD_DEPTH[cache_key] = out
        return out
        
    except Exception as e:
        logger.error(f"Error fetching depth for {instrument}: {e}")
        cached = _LAST_GOOD_DEPTH.get(cache_key)
        if cached:
            out = dict(cached)
            out["status"] = "stale"
            out["warning"] = str(e)
            out["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            return _normalize_depth_contract(
                instrument,
                out,
                mode_hint=mode_hint,
                default_status="stale",
            )
        return _normalize_depth_contract(instrument, {
            "instrument": instrument,
            "buy": [],
            "sell": [],
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "error": str(e),
            "status": "error"
        }, mode_hint=mode_hint, default_status="error")

@app.get("/api/market-data/options/{instrument}")
async def get_options_chain(instrument: str, expiry: str = None):
    """Get options chain for an instrument"""
    cache_key = f"{instrument}:{expiry or 'default'}"
    upstream_error: Optional[str] = None
    mode_hint = _get_current_mode_hint()
    try:
        # First try the Market Data API
        params = {"expiry": expiry} if expiry else {}
        try:
            response = requests.get(
                f"{MARKET_DATA_API_URL}/api/v1/options/chain/{instrument}",
                params=params,
                timeout=(2, 25)
            )
            if response.status_code == 200:
                payload = _normalize_options_contract(
                    instrument,
                    _normalize_timestamp_fields(response.json()),
                    expiry=expiry,
                    mode_hint=mode_hint,
                    default_status="ok",
                )
                if isinstance(payload, dict):
                    payload_strikes = payload.get("strikes")
                    has_strikes = bool(payload_strikes)
                    mode_hint = str(payload.get("mode_hint") or mode_hint or "").lower()
                    non_informative_historical = (
                        mode_hint == "historical"
                        and has_strikes
                        and not _options_chain_has_liquidity(payload_strikes)
                    )

                    if (not has_strikes) or non_informative_historical:
                        mode_hint = str(payload.get("mode_hint") or mode_hint or "").lower()
                        synthetic_chain = None
                        if _allow_synthetic_fallback(mode_hint):
                            synthetic_chain = _build_synthetic_options_chain_black_scholes(instrument, mode_hint=mode_hint)
                        if synthetic_chain:
                            if non_informative_historical:
                                synthetic_chain["warning"] = "Historical options chain had zero OI/volume; showing synthetic fallback."
                            else:
                                synthetic_chain["warning"] = "Upstream options chain was empty; showing synthetic fallback."
                            synthetic_chain = _normalize_options_contract(
                                instrument,
                                synthetic_chain,
                                expiry=expiry,
                                mode_hint=mode_hint,
                                default_status="synthetic",
                            )
                            _LAST_GOOD_OPTIONS[cache_key] = synthetic_chain
                            return synthetic_chain

                        payload["status"] = "no_data"
                        payload["mode_hint"] = mode_hint or "unknown"
                        payload.setdefault(
                            "message",
                            f"Options chain data is currently unavailable in {(mode_hint or 'unknown').upper()} mode for this instrument."
                        )
                        payload["warning"] = payload.get("warning") or "Upstream options API returned empty options chain."
                        return _normalize_options_contract(
                            instrument,
                            payload,
                            expiry=expiry,
                            mode_hint=mode_hint,
                            default_status="no_data",
                        )

                    payload.setdefault("status", "ok")
                    _LAST_GOOD_OPTIONS[cache_key] = payload
                return payload
            upstream_error = f"Upstream options API returned {response.status_code}"
            try:
                payload = response.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if detail:
                    upstream_error = f"{upstream_error}: {detail}"
            except Exception:
                pass
        except Exception as api_err:
            upstream_error = str(api_err)
        
        # If API doesn't have endpoint, read directly from Redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        
        instrument_upper = instrument.upper()
        keys_to_try: List[str] = []
        if expiry:
            keys_to_try.extend(
                [
                    get_redis_key(f"options:{instrument_upper}:{expiry}:chain"),
                    f"options:{instrument_upper}:{expiry}:chain",
                    f"options:{instrument}:{expiry}:chain",
                ]
            )
        else:
            keys_to_try.extend(
                [
                    get_redis_key(f"options:{instrument_upper}:chain"),
                    f"options:{instrument_upper}:chain",
                    f"options:{instrument}:chain",
                ]
            )

        options_data = None
        for options_key in keys_to_try:
            if not options_key:
                continue
            try:
                options_data = r.get(options_key)
                if options_data:
                    break
            except Exception:
                continue
        
        if not options_data:
            cached = _LAST_GOOD_OPTIONS.get(cache_key)
            if cached:
                out = dict(cached)
                out["status"] = "stale"
                out["warning"] = upstream_error or "No fresh options chain data available"
                out["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                return _normalize_options_contract(
                    instrument,
                    out,
                    expiry=expiry,
                    mode_hint=mode_hint,
                    default_status="stale",
                )

            synthetic_chain = None
            if _allow_synthetic_fallback(mode_hint):
                synthetic_chain = _build_synthetic_options_chain_black_scholes(instrument, mode_hint=mode_hint)
            if synthetic_chain:
                synthetic_chain = _normalize_options_contract(
                    instrument,
                    synthetic_chain,
                    expiry=expiry,
                    mode_hint=mode_hint,
                    default_status="synthetic",
                )
                _LAST_GOOD_OPTIONS[cache_key] = synthetic_chain
                return synthetic_chain

            if mode_hint == "historical":
                message = "Options chain data not available. This is normal for historical mode."
            elif mode_hint == "live":
                message = "Options chain data is temporarily unavailable in live mode (upstream timeout or no published chain for this instrument)."
            elif mode_hint == "paper":
                message = "Options chain data is currently unavailable in paper mode for this instrument."
            else:
                message = "Options chain data is currently unavailable for this instrument."

            # Return minimal structure for UI
            return _normalize_options_contract(instrument, {
                "instrument": instrument,
                "expiry": expiry,
                "strikes": [],
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "status": "no_data",
                "mode_hint": mode_hint,
                "message": message,
                "warning": upstream_error,
            }, expiry=expiry, mode_hint=mode_hint, default_status="no_data")
        
        out = _normalize_options_contract(
            instrument,
            _normalize_timestamp_fields(json.loads(options_data)),
            expiry=expiry,
            mode_hint=mode_hint,
            default_status="ok",
        )
        if isinstance(out, dict):
            out.setdefault("status", "ok")
            _LAST_GOOD_OPTIONS[cache_key] = out
        return out
        
    except Exception as e:
        logger.error(f"Error fetching options for {instrument}: {e}")
        cached = _LAST_GOOD_OPTIONS.get(cache_key)
        if cached:
            out = dict(cached)
            out["status"] = "stale"
            out["warning"] = str(e)
            out["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            return _normalize_options_contract(
                instrument,
                out,
                expiry=expiry,
                mode_hint=mode_hint,
                default_status="stale",
            )
        return _normalize_options_contract(instrument, {
            "instrument": instrument,
            "expiry": expiry,
            "strikes": [],
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "error": str(e),
            "status": "error"
        }, expiry=expiry, mode_hint=mode_hint, default_status="error")


# ============================================================================
# SIMPLE/FAST ENDPOINTS - Direct Redis Access (No Complex Processing)
# ============================================================================

@app.get("/simple")
async def simple_dashboard(request: Request):
    """Serve simple fast-loading dashboard."""
    from pathlib import Path
    html_path = Path(__file__).parent / "simple.html"
    with open(html_path, 'r') as f:
        content = f.read()
    return HTMLResponse(content=content)


@app.get("/api/simple/ohlc/{instrument}")
def simple_ohlc(instrument: str):
    """Get OHLC data directly from Redis - tries multiple key patterns."""
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        
        # Try multiple key patterns (live:, historical:, paper:, unprefixed)
        patterns = [
            f"live:ohlc_sorted:{instrument}:1m",
            f"ohlc_sorted:{instrument}:1m",
            f"historical:ohlc_sorted:{instrument}:1m",
            f"paper:ohlc_sorted:{instrument}:1m",
        ]
        
        for key in patterns:
            try:
                results = r.zrange(key, -50, -1)  # Last 50 bars
                if results:
                    bars = []
                    for json_data in results:
                        try:
                            bar = json.loads(json_data)
                            bars.append(bar)
                        except:
                            continue
                    if bars:
                        return JSONResponse(content=bars)
            except Exception as e:
                logger.warning(f"Failed to read {key}: {e}")
                continue
        
        # No data found
        return JSONResponse(content=[])
        
    except Exception as e:
        logger.error(f"Simple OHLC error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/api/simple/ltp/{instrument}")
async def simple_ltp(instrument: str):
    """Get LTP directly from Redis."""
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        
        # Try multiple patterns
        patterns = [f"ltp:{instrument}", f"live:ltp:{instrument}"]
        
        for key in patterns:
            try:
                data = r.get(key)
                if data:
                    return JSONResponse(content=json.loads(data))
            except:
                continue
        
        return JSONResponse(content={})
        
    except Exception as e:
        logger.error(f"Simple LTP error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/api/simple/redis-stats")
async def simple_redis_stats():
    """Get Redis connection stats."""
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        
        # Get total keys
        total_keys = r.dbsize()
        
        # Count OHLC keys
        ohlc_keys = len(list(r.scan_iter(match="*ohlc*", count=1000)))
        
        return JSONResponse(content={
            "connected": True,
            "total_keys": total_keys,
            "ohlc_keys": ohlc_keys,
            "server": f"{REDIS_HOST}:{REDIS_PORT}"
        })
        
    except Exception as e:
        logger.error(f"Redis stats error: {e}")
        return JSONResponse(content={
            "connected": False,
            "error": str(e)
        }, status_code=500)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("MARKET_DATA_DASHBOARD_PORT", "8008"))
    uvicorn.run(app, host="0.0.0.0", port=port)
