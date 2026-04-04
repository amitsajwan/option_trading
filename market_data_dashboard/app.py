#!/usr/bin/env python3
"""
Market Data Dashboard - Standalone Status and Visualization

This provides a web interface for monitoring market data status and visualization,
completely decoupled from engine/trading functionality.
"""

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import requests
import json
import csv
import asyncio
import math
from datetime import datetime, timezone, timedelta
import os
import logging
import re
from typing import Dict, Any, List, Optional, Sequence, Tuple
import time
import numpy as np
import pandas as pd
import redis
import uuid
import threading
import queue
import fnmatch
import subprocess
import sys
from collections import deque
from functools import lru_cache
from urllib.parse import quote, urlencode

try:
    from .live_strategy_monitor_service import LiveStrategyMonitorService
except ImportError:
    try:
        from live_strategy_monitor_service import LiveStrategyMonitorService  # type: ignore
    except ImportError:
        LiveStrategyMonitorService = None

try:
    from .historical_replay_monitor_service import HistoricalReplayMonitorService
except ImportError:
    try:
        from market_data_dashboard.historical_replay_monitor_service import HistoricalReplayMonitorService  # type: ignore
    except ImportError:
        HistoricalReplayMonitorService = None

try:
    from .strategy_evaluation_service import StrategyEvaluationService
except ImportError:
    try:
        from strategy_evaluation_service import StrategyEvaluationService  # type: ignore
    except ImportError:
        StrategyEvaluationService = None

try:
    from .research_eval_service import evaluate_recovery_scenario, list_recovery_scenarios
except ImportError:
    try:
        from research_eval_service import evaluate_recovery_scenario, list_recovery_scenarios  # type: ignore
    except ImportError:
        evaluate_recovery_scenario = None  # type: ignore
        list_recovery_scenarios = None  # type: ignore

try:
    from .operator_routes import DashboardOperatorRouter
except ImportError:
    from operator_routes import DashboardOperatorRouter  # type: ignore

try:
    from .historical_replay_routes import DashboardHistoricalReplayRouter
except ImportError:
    from market_data_dashboard.historical_replay_routes import DashboardHistoricalReplayRouter  # type: ignore

try:
    from .strategy_evaluation_routes import DashboardStrategyEvaluationRouter
except ImportError:
    from strategy_evaluation_routes import DashboardStrategyEvaluationRouter  # type: ignore

try:
    from .model_catalog_routes import DashboardModelCatalogRouter
except ImportError:
    from model_catalog_routes import DashboardModelCatalogRouter  # type: ignore

try:
    from .research_routes import DashboardResearchRouter
except ImportError:
    from research_routes import DashboardResearchRouter  # type: ignore

try:
    from .public_contract_routes import DashboardPublicContractRouter
except ImportError:
    from public_contract_routes import DashboardPublicContractRouter  # type: ignore

try:
    from .market_data_routes import DashboardMarketDataRouter
except ImportError:
    from market_data_routes import DashboardMarketDataRouter  # type: ignore

try:
    from .debug_routes import DashboardDebugRouter
except ImportError:
    from debug_routes import DashboardDebugRouter  # type: ignore

try:
    from .legacy_trading_runtime_routes import DashboardLegacyTradingRouter
except ImportError:
    from legacy_trading_runtime_routes import DashboardLegacyTradingRouter  # type: ignore

try:
    from snapshot_app.core.snapshot_ml_flat_contract import load_contract_schema, load_feature_groups, load_legacy_mapping
except ImportError:
    load_contract_schema = None  # type: ignore
    load_feature_groups = None  # type: ignore
    load_legacy_mapping = None  # type: ignore

try:
    from .runtime_artifacts import load_strategy_runtime_observability
except ImportError:
    try:
        from runtime_artifacts import load_strategy_runtime_observability  # type: ignore
    except ImportError:
        load_strategy_runtime_observability = None  # type: ignore

try:
    from contracts_app.options_math import black_scholes_price, calculate_option_greeks, estimate_risk_free_rate
except ImportError:
    black_scholes_price = None
    calculate_option_greeks = None
    estimate_risk_free_rate = None

try:
    from contracts_app import IST_ZONE, TimestampSourceMode, isoformat_ist, parse_timestamp_to_ist
except ImportError:
    from zoneinfo import ZoneInfo

    IST_ZONE = ZoneInfo("Asia/Kolkata")
    TimestampSourceMode = None  # type: ignore
    isoformat_ist = None  # type: ignore
    parse_timestamp_to_ist = None  # type: ignore

try:
    from ingestion_app.env_settings import redis_config as _redis_env_config, resolve_instrument_symbol
except ImportError:
    _redis_env_config = None
    resolve_instrument_symbol = None

try:
    from contracts_app import get_redis_key
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
_PLACEHOLDER_INSTRUMENTS = {"", "FALLBACK_TEST", "SELECT_INSTRUMENT", "INSTRUMENT_NOT_SET"}


def _is_placeholder_instrument(value: Any) -> bool:
    return str(value or "").strip().upper() in _PLACEHOLDER_INSTRUMENTS


def _normalize_instrument_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text


def _infer_exchange_for_symbol(symbol: str) -> Optional[str]:
    normalized = _normalize_instrument_symbol(symbol)
    if not normalized:
        return None
    if normalized.endswith(("FUT", "CE", "PE")):
        return "NFO"
    if normalized in {"INDIA VIX", "INDIAVIX", "BANKNIFTY", "NIFTY", "NIFTY BANK", "NIFTY 50"}:
        return "NSE"
    return None


def _normalize_instrument_entry(raw: Any) -> Optional[Dict[str, Optional[str]]]:
    if isinstance(raw, dict):
        symbol = _normalize_instrument_symbol(raw.get("symbol"))
        exchange = str(raw.get("exchange") or "").strip().upper() or None
    else:
        symbol = _normalize_instrument_symbol(raw)
        exchange = None
    if not symbol or _is_placeholder_instrument(symbol):
        return None
    return {
        "symbol": symbol,
        "exchange": exchange or _infer_exchange_for_symbol(symbol),
    }

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
    """Parse various timestamp representations into a timezone-aware datetime (IST)."""
    if parse_timestamp_to_ist is not None:
        parsed = parse_timestamp_to_ist(value, naive_mode=TimestampSourceMode.MARKET_IST)
        if parsed is not None:
            return parsed
    if value is None:
        return None

    # Numeric epoch support (seconds or milliseconds)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=IST_ZONE)
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
                return datetime.fromtimestamp(num, tz=IST_ZONE)
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
            dt = dt.replace(tzinfo=IST_ZONE)
        return dt.astimezone(IST_ZONE)

    return None


def _normalize_timestamp_string(value: Any) -> Any:
    """Normalize a timestamp-like value to ISO-8601 IST string when parseable."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        # Preserve date-only fields as-is; converting through timezone creates confusing day shifts.
        return value
    dt = _parse_timestamp_flexible(value)
    if not dt:
        return value
    if isoformat_ist is not None:
        return isoformat_ist(dt)
    return dt.astimezone(IST_ZONE).isoformat()


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
            elif (
                key_l == "timestamp"
                or key_l.endswith("_timestamp")
                or key_l.endswith("_time")
                or key_l.endswith("_date")
                or key_l.endswith("_at")
                or key_l in {"entry_dt", "exit_dt", "started_at", "ended_at", "submitted_at", "updated_at"}
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


def _fetch_api_instrument_fallback(symbol: str) -> Optional[Dict[str, Any]]:
    """Best-effort API fallback when Redis OHLC history is absent for an instrument."""
    normalized_symbol = _normalize_instrument_symbol(symbol)
    if not normalized_symbol:
        return None

    encoded_symbol = quote(normalized_symbol, safe="")
    tick_payload: Optional[Dict[str, Any]] = None
    ohlc_payload: List[Dict[str, Any]] = []

    try:
        tick_response = requests.get(
            f"{MARKET_DATA_API_URL}/api/v1/market/tick/{encoded_symbol}",
            timeout=2,
        )
        if tick_response.status_code == 200:
            payload = tick_response.json()
            if isinstance(payload, dict):
                tick_payload = payload
    except Exception:
        pass

    try:
        ohlc_response = requests.get(
            f"{MARKET_DATA_API_URL}/api/v1/market/ohlc/{encoded_symbol}?timeframe=1m&limit=2&order=desc",
            timeout=3,
        )
        if ohlc_response.status_code == 200:
            payload = ohlc_response.json()
            if isinstance(payload, list):
                ohlc_payload = [item for item in payload if isinstance(item, dict)]
    except Exception:
        pass

    if not tick_payload and not ohlc_payload:
        return None

    latest_bar = ohlc_payload[0] if ohlc_payload else {}
    oldest_bar = ohlc_payload[-1] if ohlc_payload else latest_bar
    latest_timestamp = (
        _extract_bar_timestamp(latest_bar)
        or (tick_payload or {}).get("timestamp")
    )
    first_timestamp = (
        _extract_bar_timestamp(oldest_bar)
        or latest_timestamp
    )
    latest_price = (
        latest_bar.get("close")
        or latest_bar.get("last_price")
        or (tick_payload or {}).get("last_price")
    )

    return {
        "status": "available",
        "data_points": len(ohlc_payload) if ohlc_payload else (1 if tick_payload else 0),
        "first_timestamp": _normalize_timestamp_string(first_timestamp),
        "latest_timestamp": _normalize_timestamp_string(latest_timestamp),
        "latest_price": latest_price,
        "data_source": "api_fallback",
        "tick_timestamp": _normalize_timestamp_string((tick_payload or {}).get("timestamp")),
    }


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


def _canonical_indicator_timeframe(raw: Any) -> str:
    text = str(raw or "1min").strip().lower()
    aliases = {
        "1m": "1min",
        "1min": "1min",
        "minute": "1min",
        "5m": "5min",
        "5min": "5min",
        "15m": "15min",
        "15min": "15min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }
    return aliases.get(text, text or "1min")


def _indicator_stale_threshold_seconds(timeframe: str) -> int:
    tf = _canonical_indicator_timeframe(timeframe)
    return {
        "1min": 180,
        "5min": 900,
        "15min": 1800,
        "1h": 5400,
        "4h": 21600,
        "1d": 86400,
    }.get(tf, 180)


def _has_indicator_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        return bool(text and text != "--")
    if isinstance(value, (int, float)):
        return not (math.isnan(float(value)) or math.isinf(float(value)))
    return True


def _rsi_status_label(rsi_value: Optional[float]) -> Optional[str]:
    if rsi_value is None or not math.isfinite(rsi_value):
        return None
    if rsi_value >= 70.0:
        return "OVERBOUGHT"
    if rsi_value <= 30.0:
        return "OVERSOLD"
    if rsi_value >= 55.0:
        return "BULLISH"
    if rsi_value <= 45.0:
        return "BEARISH"
    return "NEUTRAL"


def _volatility_level_from_metrics(
    *,
    atr: Optional[float],
    price: Optional[float],
    realized_vol_30m: Optional[float],
) -> Optional[str]:
    rv = _safe_float(realized_vol_30m, None)
    if rv is not None and math.isfinite(rv):
        if rv >= 0.18:
            return "HIGH"
        if rv >= 0.10:
            return "MEDIUM"
        return "LOW"
    if atr is None or price is None or not math.isfinite(atr) or not math.isfinite(price) or price == 0:
        return None
    ratio = abs(float(atr) / float(price))
    if ratio >= 0.010:
        return "HIGH"
    if ratio >= 0.004:
        return "MEDIUM"
    return "LOW"


def _snapshot_to_indicator_fields(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    session_levels = snapshot.get("session_levels") if isinstance(snapshot.get("session_levels"), dict) else {}
    futures_bar = snapshot.get("futures_bar") if isinstance(snapshot.get("futures_bar"), dict) else {}
    futures_derived = snapshot.get("futures_derived") if isinstance(snapshot.get("futures_derived"), dict) else {}
    mtf_derived = snapshot.get("mtf_derived") if isinstance(snapshot.get("mtf_derived"), dict) else {}
    opening_range = snapshot.get("opening_range") if isinstance(snapshot.get("opening_range"), dict) else {}

    oi = _safe_float(futures_bar.get("fut_oi"), None)
    oi_change = _safe_float(futures_derived.get("fut_oi_change_30m"), None)
    oi_pct_change = None
    if oi is not None and oi_change is not None:
        prev_oi = oi - oi_change
        if prev_oi != 0:
            oi_pct_change = (oi_change / prev_oi) * 100.0

    pivot_point = pivot_r1 = pivot_s1 = None
    prev_high = _safe_float(session_levels.get("prev_day_high"), None)
    prev_low = _safe_float(session_levels.get("prev_day_low"), None)
    prev_close = _safe_float(session_levels.get("prev_day_close"), None)
    if prev_high is not None and prev_low is not None and prev_close is not None:
        pivot_point = (prev_high + prev_low + prev_close) / 3.0
        pivot_r1 = (2.0 * pivot_point) - prev_low
        pivot_s1 = (2.0 * pivot_point) - prev_high

    rsi_14 = _safe_float(mtf_derived.get("rsi_14_1m"), None)
    trend_direction = str(mtf_derived.get("ema_trend_5m") or "").strip().upper() or None

    out = {
        "rsi_14": rsi_14,
        "rsi_status": _rsi_status_label(rsi_14),
        "macd_value": _safe_float(mtf_derived.get("macd_line_5m"), None),
        "trend_direction": trend_direction,
        "atr_14": _safe_float(mtf_derived.get("atr_14_1m"), None),
        "bollinger_percent_b": _safe_float(mtf_derived.get("bb_pct_b_5m"), None),
        "volatility_level": _volatility_level_from_metrics(
            atr=_safe_float(mtf_derived.get("atr_14_1m"), None),
            price=_safe_float(futures_bar.get("fut_close"), None),
            realized_vol_30m=_safe_float(futures_derived.get("realized_vol_30m"), None),
        ),
        "pivot_point": pivot_point,
        "pivot_r1": pivot_r1,
        "pivot_s1": pivot_s1,
        "range_20": _safe_float(opening_range.get("or_width"), None),
        "oi": oi,
        "oi_change": oi_change,
        "oi_pct_change": oi_pct_change,
        "oi_sma_5": oi,
    }
    return {k: v for k, v in out.items() if _has_indicator_value(v)}


def _extract_snapshot_timestamp(snapshot: Dict[str, Any], fallback_ts: Any = None) -> Optional[str]:
    session_context = snapshot.get("session_context") if isinstance(snapshot.get("session_context"), dict) else {}
    raw = (
        session_context.get("timestamp")
        or session_context.get("time")
        or fallback_ts
    )
    normalized = _normalize_timestamp_string(raw)
    return str(normalized) if normalized else None


def _load_latest_snapshot_from_mongo(instrument: str) -> Optional[Dict[str, Any]]:
    if _strategy_eval_service is None:
        return None
    try:
        coll_name = str(os.getenv("MONGO_COLL_SNAPSHOTS") or "phase1_market_snapshots").strip() or "phase1_market_snapshots"
        coll = _strategy_eval_service._db()[coll_name]
        query = {"instrument": str(instrument).strip().upper()}
        projection = {
            "_id": 0,
            "timestamp": 1,
            "trade_date_ist": 1,
            "payload.snapshot": 1,
        }
        # Some historical rows may carry mixed timestamp formats/types.
        # trade_date_ist is normalized YYYY-MM-DD and sorts reliably.
        doc = coll.find_one(
            query,
            projection=projection,
            sort=[("trade_date_ist", -1), ("timestamp", -1)],
        )
        if not isinstance(doc, dict):
            return None
        payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else {}
        snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
        if not snapshot:
            return None
        return {
            "snapshot": snapshot,
            "snapshot_timestamp": _normalize_timestamp_string(doc.get("timestamp")),
            "trade_date_ist": doc.get("trade_date_ist"),
            "source": "mongo_snapshots",
        }
    except Exception:
        return None


def _load_latest_historical_snapshot_from_mongo(instrument: str) -> Optional[Dict[str, Any]]:
    if _strategy_eval_service is None:
        return None
    try:
        coll_name = (
            str(os.getenv("MONGO_COLL_SNAPSHOTS_HISTORICAL") or "phase1_market_snapshots_historical").strip()
            or "phase1_market_snapshots_historical"
        )
        coll = _strategy_eval_service._db()[coll_name]
        query: Dict[str, Any] = {}
        symbol = str(instrument or "").strip()
        if symbol:
            query["instrument"] = symbol
        vt = get_virtual_time_info()
        current_time = vt.get("current_time") if isinstance(vt, dict) else None
        if current_time is not None:
            query["timestamp"] = {"$lte": _normalize_timestamp_string(current_time)}
            query["trade_date_ist"] = current_time.astimezone(IST_ZONE).date().isoformat()
        projection = {
            "_id": 0,
            "instrument": 1,
            "timestamp": 1,
            "trade_date_ist": 1,
            "payload.snapshot": 1,
        }
        doc = coll.find_one(
            query,
            projection=projection,
            sort=[("trade_date_ist", -1), ("timestamp", -1)],
        )
        if not isinstance(doc, dict) and symbol:
            query.pop("instrument", None)
            doc = coll.find_one(
                query,
                projection=projection,
                sort=[("trade_date_ist", -1), ("timestamp", -1)],
            )
        if not isinstance(doc, dict):
            return None
        payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else {}
        snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
        if not snapshot:
            return None
        return {
            "instrument": str(doc.get("instrument") or snapshot.get("instrument") or "").strip() or None,
            "snapshot": snapshot,
            "snapshot_timestamp": _normalize_timestamp_string(doc.get("timestamp")),
            "trade_date_ist": doc.get("trade_date_ist"),
            "source": "mongo_snapshots_historical",
        }
    except Exception:
        return None


def _historical_options_payload_from_snapshot(instrument: str) -> Optional[Dict[str, Any]]:
    selected_snapshot = _load_latest_historical_snapshot_from_mongo(instrument)
    snapshot = (
        selected_snapshot.get("snapshot")
        if isinstance(selected_snapshot, dict) and isinstance(selected_snapshot.get("snapshot"), dict)
        else {}
    )
    if not snapshot:
        return None
    strikes = snapshot.get("strikes")
    if not isinstance(strikes, list) or not strikes:
        return None
    chain_aggregates = snapshot.get("chain_aggregates") if isinstance(snapshot.get("chain_aggregates"), dict) else {}
    futures_bar = snapshot.get("futures_bar") if isinstance(snapshot.get("futures_bar"), dict) else {}
    timestamp = _extract_snapshot_timestamp(snapshot, fallback_ts=(selected_snapshot or {}).get("snapshot_timestamp"))
    return {
        "instrument": str((selected_snapshot or {}).get("instrument") or snapshot.get("instrument") or instrument).strip() or instrument,
        "timestamp": timestamp or _now_iso_ist(),
        "source": "mongo_snapshots_historical",
        "mode_hint": "historical",
        "status": "ok",
        "strikes": _json_safe_value(strikes),
        "futures_price": _coerce_float(futures_bar.get("fut_close")),
        "underlying_price": _coerce_float(futures_bar.get("fut_close")),
        "pcr": _coerce_float(chain_aggregates.get("pcr")),
        "max_pain": chain_aggregates.get("max_pain"),
        "chain_aggregates": _json_safe_value(chain_aggregates),
        "atm_options": _json_safe_value(snapshot.get("atm_options")),
    }


def _redis_get_first_value(
    r: redis.Redis,
    keys: Sequence[str],
) -> Tuple[Optional[str], Optional[str]]:
    for key in keys:
        try:
            value = r.get(str(key))
        except Exception:
            continue
        if value is not None:
            return str(key), str(value)
    return None, None


def _redis_prefixed_keys(mode_hint: Optional[str], suffixes: Sequence[str]) -> List[str]:
    out: List[str] = []
    for mode in _mode_priority(mode_hint):
        for suffix in suffixes:
            out.append(f"{mode}:{suffix}")
    out.extend([str(s) for s in suffixes])
    # stable de-dup
    return list(dict.fromkeys(out))


def _mongo_latest_ts_for_instrument(
    coll_name: str,
    instrument: str,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "collection": coll_name,
        "collection_exists": False,
        "timestamp": None,
        "has_data": False,
    }
    if _strategy_eval_service is None:
        out["error"] = "strategy_evaluation_service_unavailable"
        return out
    try:
        db = _strategy_eval_service._db()
        existing = set(db.list_collection_names())
        if coll_name not in existing:
            return out
        out["collection_exists"] = True
        query = {"instrument": str(instrument).strip().upper()}
        doc = db[coll_name].find_one(query, projection={"_id": 0, "timestamp": 1}, sort=[("timestamp", -1)])
        if isinstance(doc, dict):
            out["timestamp"] = _normalize_timestamp_string(doc.get("timestamp"))
            out["has_data"] = True
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out


def _lag_check_payload(
    *,
    name: str,
    redis_timestamp: Any,
    mongo_timestamp: Any,
    threshold_seconds: int,
    redis_source: Optional[str] = None,
    mongo_source: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    redis_norm = _normalize_timestamp_string(redis_timestamp)
    mongo_norm = _normalize_timestamp_string(mongo_timestamp)
    redis_dt = _parse_timestamp_flexible(redis_norm)
    mongo_dt = _parse_timestamp_flexible(mongo_norm)

    raw_delta_seconds: Optional[float] = None
    mongo_lag_seconds: Optional[float] = None
    if redis_dt is not None and mongo_dt is not None:
        raw_delta_seconds = (redis_dt - mongo_dt).total_seconds()
        mongo_lag_seconds = max(0.0, raw_delta_seconds)

    if redis_dt is None and mongo_dt is None:
        status = "no_data"
    elif redis_dt is None:
        status = "redis_missing"
    elif mongo_dt is None:
        status = "mongo_missing"
    elif mongo_lag_seconds is not None and mongo_lag_seconds > float(threshold_seconds):
        status = "lagging"
    else:
        status = "ok"

    return {
        "name": name,
        "status": status,
        "threshold_seconds": int(max(1, threshold_seconds)),
        "redis_timestamp": redis_norm,
        "mongo_timestamp": mongo_norm,
        "raw_delta_seconds": raw_delta_seconds,
        "mongo_lag_seconds": mongo_lag_seconds,
        "redis_source": redis_source,
        "mongo_source": mongo_source,
        "note": note,
    }


logger = logging.getLogger(__name__)

app = FastAPI(
    title="Market Data Dashboard",
    description="Standalone market data monitoring and visualization",
    version="1.0.0"
)

_cors_default = ",".join(
    [
        "http://localhost:8011",
        "http://127.0.0.1:8011",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
)
_cors_raw = str(os.getenv("DASHBOARD_CORS_ORIGINS") or _cors_default)
_cors_origins = [item.strip() for item in _cors_raw.split(",") if item.strip()]
_cors_allow_all = "*" in _cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_allow_all else _cors_origins,
    allow_credentials=False if _cors_allow_all else True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_strategy_eval_service = StrategyEvaluationService() if StrategyEvaluationService is not None else None
_live_strategy_monitor_service = (
    LiveStrategyMonitorService(_strategy_eval_service)
    if (LiveStrategyMonitorService is not None and _strategy_eval_service is not None)
    else None
)
_historical_replay_monitor_service = (
    HistoricalReplayMonitorService(_strategy_eval_service)
    if HistoricalReplayMonitorService is not None
    else None
)

# Mount static files (optional - create directory if needed)
import os
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Setup templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Market Data API configuration
MARKET_DATA_API_URL = os.getenv("MARKET_DATA_API_URL") or (
    f"http://{os.getenv('MARKET_DATA_API_HOST', 'localhost')}:"
    f"{os.getenv('MARKET_DATA_API_PORT', '8004')}"
)

# Lightweight in-memory caches for selected API payloads.
_LAST_GOOD_DEPTH: Dict[str, Dict[str, Any]] = {}
_LAST_GOOD_OPTIONS: Dict[str, Dict[str, Any]] = {}

PUBLIC_SCHEMA_VERSION = "v1"
PUBLIC_TOPICS: Tuple[str, ...] = ("mode", "tick", "ohlc", "indicators", "depth", "options", "strategy_eval")
PUBLIC_TIMEFRAMES: Tuple[str, ...] = ("1m", "5m", "15m")
PUBLIC_TIMEFRAME_ALIASES: Dict[str, List[str]] = {
    "1m": ["1m", "1min", "minute"],
    "5m": ["5m", "5min"],
    "15m": ["15m", "15min"],
}

# Trading terminal runtime state (paper runner process managed by dashboard UI).
REPO_ROOT = Path(__file__).parent.parent
ML_PIPELINE_SRC = REPO_ROOT / "ml_pipeline" / "src"
_LEGACY_TRADING_ARTIFACTS_DIR = REPO_ROOT / ".run" / "dashboard_state"
DEFAULT_TRADING_EVENTS_PATH = _LEGACY_TRADING_ARTIFACTS_DIR / "t33_paper_capital_events_actual.jsonl"
DEFAULT_TRADING_STDOUT_PATH = _LEGACY_TRADING_ARTIFACTS_DIR / "t33_paper_capital_runner_stdout.log"
DEFAULT_TRADING_STDERR_PATH = _LEGACY_TRADING_ARTIFACTS_DIR / "t33_paper_capital_runner_stderr.log"
DEFAULT_MODEL_EVAL_SUMMARY_PATH = None
DEFAULT_MODEL_TRAINING_REPORT_PATH = None
DEFAULT_MODEL_POLICY_REPORT_PATH = None
TRADING_MODEL_CATALOG_DIR = REPO_ROOT / "ml_pipeline_2" / "artifacts" / "published_models"
ML_PIPELINE_2_ARTIFACT_MODEL_CATALOG_DIR = REPO_ROOT / "ml_pipeline_2" / "artifacts" / "published_models"
SNAPSHOT_ML_FLAT_CONTRACT_DIR = REPO_ROOT / "snapshot_app" / "contracts" / "snapshot_ml_flat"
LEGACY_TRADING_RUNTIME_ENV = "ENABLE_LEGACY_TRADING_UI"

_TRADING_LOCK = threading.Lock()
_TRADING_DEFAULT_INSTANCE = "default"
_TRADING_RUNNERS: Dict[str, Dict[str, Any]] = {}
_TRADING_LAST_BACKTEST: Dict[str, Dict[str, Any]] = {}
_TRADING_BACKTEST_STATE_DIR = REPO_ROOT / ".run" / "dashboard_state"


def _legacy_trading_runtime_requested() -> bool:
    return _truthy(os.getenv(LEGACY_TRADING_RUNTIME_ENV), default=False)


def _legacy_trading_runtime_status() -> Dict[str, Any]:
    requested = _legacy_trading_runtime_requested()
    package_present = ML_PIPELINE_SRC.exists()
    enabled = bool(requested and package_present)
    if enabled:
        detail = "Legacy paper-trading launcher is enabled explicitly for this dashboard."
    elif package_present:
        detail = (
            "Legacy paper-trading launcher is disabled by default on this branch. "
            f"Set {LEGACY_TRADING_RUNTIME_ENV}=1 only if you intentionally need the archived paper/backtest workflow."
        )
    else:
        detail = (
            "Legacy paper-trading launcher is unavailable because deprecated ml_pipeline runtime code is not present."
        )
    return {
        "enabled": enabled,
        "requested": requested,
        "package_present": package_present,
        "env_var": LEGACY_TRADING_RUNTIME_ENV,
        "detail": detail,
    }


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


def _read_csv_dict_rows(path: Path) -> List[Dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except Exception:
        return []


def _humanize_identifier_token(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    token_map = {
        "atr": "ATR",
        "atm": "ATM",
        "ce": "CE",
        "ctx": "Context",
        "dte": "DTE",
        "ema": "EMA",
        "fut": "Fut",
        "iv": "IV",
        "oi": "OI",
        "opt": "Opt",
        "osc": "Osc",
        "pcr": "PCR",
        "pe": "PE",
        "px": "Price",
        "ret": "Return",
        "rsi": "RSI",
        "vix": "VIX",
        "vwap": "VWAP",
    }
    lowered = raw.lower()
    if lowered in token_map:
        return token_map[lowered]
    if raw.isdigit():
        return raw
    if raw[:-1].isdigit() and raw.endswith("m"):
        return raw
    if raw[:-1].isdigit() and raw.endswith("d"):
        return raw.upper()
    if len(raw) <= 3 and raw.isalpha():
        return raw.upper()
    return raw.replace("-", " ").capitalize()


def _humanize_model_catalog_title(model_group: str, profile_id: Optional[str] = None) -> str:
    base = " / ".join(
        " ".join(_humanize_identifier_token(part) for part in section.split("_") if str(part).strip())
        for section in str(model_group or "").split("/")
        if str(section).strip()
    )
    suffix = str(profile_id or "").strip()
    if not suffix:
        return base or "Published Model"
    pretty_suffix = " ".join(_humanize_identifier_token(part) for part in suffix.split("_") if str(part).strip())
    return f"{base} [{pretty_suffix}]" if base else pretty_suffix


def _format_catalog_number(value: Any, *, digits: int = 2) -> str:
    try:
        number = float(value)
    except Exception:
        return "--"
    if not math.isfinite(number):
        return "--"
    return f"{number:.{digits}f}"


def _format_catalog_percent(value: Any, *, digits: int = 2) -> str:
    try:
        number = float(value)
    except Exception:
        return "--"
    if not math.isfinite(number):
        return "--"
    return f"{number * 100:.{digits}f}%"


def _format_catalog_int(value: Any) -> str:
    try:
        number = int(value)
    except Exception:
        return "--"
    return f"{number:d}"


def _catalog_metric_card(label: str, value: str) -> Dict[str, str]:
    return {"label": str(label or "").strip(), "value": str(value or "--").strip() or "--"}


def _catalog_path_row(label: str, path: Optional[Path]) -> Dict[str, Any]:
    return {
        "label": str(label or "").strip(),
        "path": _path_text(path),
        "exists": bool(path and path.exists()),
    }


def _catalog_action(label: str, url: str, *, primary: bool = False) -> Optional[Dict[str, Any]]:
    text = str(label or "").strip()
    href = str(url or "").strip()
    if not text or not href:
        return None
    return {"label": text, "url": href, "primary": bool(primary)}


def _default_catalog_actions(
    *,
    prefill_url: str,
    launch_url: str,
    evaluation_api_url: str,
    supports_terminal: bool,
    research_url: str = "",
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    if research_url:
        action = _catalog_action("Open Research Explorer", research_url, primary=True)
        if action:
            actions.append(action)
    if supports_terminal:
        action = _catalog_action("Open Prefilled Terminal", prefill_url, primary=not actions)
        if action:
            actions.append(action)
        action = _catalog_action("Open By Instance", launch_url)
        if action:
            actions.append(action)
    action = _catalog_action("Open Eval JSON", evaluation_api_url)
    if action:
        actions.append(action)
    return actions


@lru_cache(maxsize=1)
def _load_snapshot_ml_flat_dictionary() -> Dict[str, Any]:
    groups_payload = load_feature_groups(SNAPSHOT_ML_FLAT_CONTRACT_DIR) if load_feature_groups is not None else {}
    schema_payload = load_contract_schema(SNAPSHOT_ML_FLAT_CONTRACT_DIR) if load_contract_schema is not None else {}
    mapping_frame = load_legacy_mapping(SNAPSHOT_ML_FLAT_CONTRACT_DIR) if load_legacy_mapping is not None else pd.DataFrame()
    mapping_rows = mapping_frame.to_dict(orient="records") if isinstance(mapping_frame, pd.DataFrame) else []

    group_payloads = groups_payload.get("groups") if isinstance(groups_payload, dict) else {}
    group_order: List[str] = []
    groups: Dict[str, Dict[str, Any]] = {}
    column_to_group: Dict[str, str] = {}
    field_labels: Dict[str, str] = {}
    for group_key, payload in (group_payloads or {}).items():
        key = str(group_key or "").strip()
        if not key or not isinstance(payload, dict):
            continue
        label = str(payload.get("label") or key).strip() or key
        columns = [str(col).strip() for col in payload.get("columns", []) if str(col).strip()]
        groups[key] = {"label": label, "columns": columns}
        group_order.append(key)
        prefix = f"{key}_"
        for column in columns:
            column_to_group[column] = key
            trimmed = column[len(prefix):] if column.startswith(prefix) else column
            field_labels[column] = " ".join(_humanize_identifier_token(part) for part in trimmed.split("_") if part)

    rename_map: Dict[str, str] = {}
    removed_legacy: set[str] = set()
    for row in mapping_rows:
        legacy_name = str(row.get("legacy_name") or "").strip()
        new_name = str(row.get("new_name") or "").strip()
        is_removed = str(row.get("is_removed") or "").strip().lower() == "true"
        if not legacy_name:
            continue
        if is_removed:
            removed_legacy.add(legacy_name)
            continue
        if new_name:
            rename_map[legacy_name] = new_name

    schema_fields = schema_payload.get("fields") if isinstance(schema_payload, dict) else []
    required_columns = schema_payload.get("required_columns") if isinstance(schema_payload, dict) else []
    return {
        "contract_id": str(schema_payload.get("contract_id") or groups_payload.get("contract_id") or "snapshot_ml_flat"),
        "schema_version": str(schema_payload.get("schema_version") or groups_payload.get("schema_version") or "unknown"),
        "group_order": group_order,
        "groups": groups,
        "column_to_group": column_to_group,
        "field_labels": field_labels,
        "rename_map": rename_map,
        "removed_legacy": removed_legacy,
        "schema_fields": schema_fields if isinstance(schema_fields, list) else [],
        "required_columns": required_columns if isinstance(required_columns, list) else [],
    }


def _discover_latest_profile_paths(model_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
    profiles_dir = model_dir / "config" / "profiles"
    if not profiles_dir.exists():
        return None, None
    training_reports = sorted(
        profiles_dir.glob("*/training_report.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    threshold_reports = sorted(
        profiles_dir.glob("*/threshold_report.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return (
        training_reports[0] if training_reports else None,
        threshold_reports[0] if threshold_reports else None,
    )


def _discover_published_model_roots(root: Path, *, source_label: str) -> List[Dict[str, Any]]:
    if not root.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for model_contract_path in sorted(root.rglob("model_contract.json")):
        model_dir = model_contract_path.parent
        latest_payload = _safe_load_json(model_dir / "reports" / "training" / "latest.json") or {}
        published_raw = latest_payload.get("published_paths") if isinstance(latest_payload, dict) else {}
        published = published_raw if isinstance(published_raw, dict) else {}
        training_fallback, threshold_fallback = _discover_latest_profile_paths(model_dir)

        model_group = str(latest_payload.get("model_group") or "").strip()
        if not model_group:
            try:
                rel = model_dir.relative_to(root)
                model_group = rel.as_posix()
            except Exception:
                model_group = model_dir.name
        profile_id = str(latest_payload.get("profile_id") or "").strip()
        run_id = str(latest_payload.get("run_id") or "").strip()
        feature_profile = str(latest_payload.get("feature_profile") or "").strip()
        if not feature_profile:
            feature_profile = str(model_group.split("/", 1)[0]).strip()

        raw_entry = {
            "instance_key": _normalize_trading_instance(model_group.replace("/", "_")),
            "profile_key": profile_id or _normalize_trading_instance(model_group),
            "title": _humanize_model_catalog_title(model_group, profile_id),
            "summary": f"Published artifact discovery for {model_group}",
            "description": f"run={run_id or '--'} profile={profile_id or '--'}",
            "recommended": False,
            "model_group": model_group,
            "profile_id": profile_id,
            "run_id": run_id,
            "feature_profile": feature_profile,
            "model_package": str(
                published.get("model_package")
                or _path_text(model_dir / "model" / "model.joblib")
            ),
            "threshold_report": str(
                published.get("threshold_report")
                or _path_text(threshold_fallback)
            ),
            "training_report_path": str(
                published.get("training_report")
                or _path_text(training_fallback)
            ),
            "model_contract": _path_text(model_contract_path),
        }
        entries.append(_build_catalog_entry(raw_entry, source=source_label, load_eval_snapshot=False))
    return entries


def _recovery_discovery_roots() -> Tuple[Path, ...]:
    roots = (
        REPO_ROOT / "artifacts",
        REPO_ROOT / "ml_pipeline_2" / "artifacts",
    )
    unique: List[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except Exception:
            resolved = root
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return tuple(unique)


def _list_recovery_scenarios_for_dashboard() -> Any:
    if list_recovery_scenarios is None:
        raise RuntimeError("research evaluation service unavailable")
    roots = _recovery_discovery_roots()
    try:
        return list_recovery_scenarios(roots=roots)
    except TypeError:
        return list_recovery_scenarios()


def _evaluate_recovery_scenario_for_dashboard(**kwargs: Any) -> Any:
    if evaluate_recovery_scenario is None:
        raise RuntimeError("research evaluation service unavailable")
    roots = _recovery_discovery_roots()
    try:
        return evaluate_recovery_scenario(**kwargs, roots=roots)
    except TypeError:
        return evaluate_recovery_scenario(**kwargs)


def _build_recovery_catalog_entries() -> List[Dict[str, Any]]:
    if list_recovery_scenarios is None:
        return []
    try:
        payload = list_recovery_scenarios(roots=_recovery_discovery_roots())
    except Exception:
        return []

    scenarios = list(payload.get("scenarios") or []) if isinstance(payload, dict) else []
    entries: List[Dict[str, Any]] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        scenario_key = str(scenario.get("scenario_key") or "").strip()
        run_dir = _resolve_repo_path(scenario.get("run_dir"))
        if not scenario_key or not isinstance(run_dir, Path):
            continue

        feature_sets = [str(item).strip() for item in scenario.get("feature_sets", []) if str(item).strip()]
        primary_model = str(scenario.get("primary_model") or "").strip()
        eval_window = dict(scenario.get("eval_window") or {})
        default_from = str(eval_window.get("default_start") or eval_window.get("allowed_start") or "").strip()
        default_to = str(eval_window.get("default_end") or eval_window.get("allowed_end") or "").strip()
        run_name = str(scenario.get("run_name") or scenario.get("title") or run_dir.name).strip() or run_dir.name
        if run_name.lower() == "run" or run_name == run_dir.name:
            parent_name = str(run_dir.parent.name or "").strip()
            if parent_name:
                run_name = parent_name
        created_at_utc = str(scenario.get("created_at_utc") or "").strip()

        for recipe_meta in list(scenario.get("recipes") or []):
            if not isinstance(recipe_meta, dict):
                continue
            recipe_id = str(recipe_meta.get("recipe_id") or "").strip()
            if not recipe_id:
                continue

            holdout_metrics = dict(recipe_meta.get("holdout_metrics") or {})
            stage_a_passed = bool(holdout_metrics.get("stage_a_passed"))
            side_share_in_band = bool(holdout_metrics.get("side_share_in_band"))
            recommended_threshold = recipe_meta.get("recommended_threshold")
            if recommended_threshold is None:
                recommended_threshold = recipe_meta.get("default_threshold")

            query_values: Dict[str, str] = {
                "scenario_key": scenario_key,
                "recipe_id": recipe_id,
            }
            if default_from:
                query_values["date_from"] = default_from
            if default_to:
                query_values["date_to"] = default_to
            if recommended_threshold is not None:
                query_values["threshold"] = str(recommended_threshold)

            recipe_dir = run_dir / "primary_recipes" / recipe_id
            model_path = recipe_dir / "model.joblib"
            recipe_summary_path = recipe_dir / "summary.json"
            training_path = recipe_dir / "training_report.json"
            threshold_sweep_path = recipe_dir / "threshold_sweep" / "summary.json"

            title = run_name if recipe_id.lower() in run_name.lower() else f"{run_name} [{recipe_id}]"
            feature_set_text = ", ".join(feature_sets) if feature_sets else "--"
            net_return_sum = holdout_metrics.get("net_return_sum")
            profit_factor = holdout_metrics.get("profit_factor")
            trades = holdout_metrics.get("trades")
            win_rate = holdout_metrics.get("win_rate")
            long_share = holdout_metrics.get("long_share")

            actions = _default_catalog_actions(
                prefill_url="",
                launch_url="",
                evaluation_api_url=f"/api/trading/research/evaluation?{urlencode(query_values)}",
                supports_terminal=False,
                research_url=f"/trading/research?{urlencode(query_values)}",
            )
            entries.append(
                {
                    "catalog_kind": "recovery",
                    "source": "artifact_discovery_recovery",
                    "source_label": "recovery research",
                    "instance_key": _normalize_trading_instance(f"{scenario_key}_{recipe_id}"),
                    "profile_key": recipe_id,
                    "model_group": primary_model,
                    "profile_id": recipe_id,
                    "run_id": run_dir.name,
                    "feature_profile": feature_set_text,
                    "title": title,
                    "summary": f"Recovery model - {feature_set_text} - {primary_model or '--'}",
                    "description": (
                        f"Recipe {recipe_id} - Created {created_at_utc or '--'} - "
                        f"Trades {_format_catalog_int(trades)} - Net {_format_catalog_percent(net_return_sum)} - "
                        f"PF {_format_catalog_number(profit_factor)}"
                    ),
                    "recommended": stage_a_passed and side_share_in_band and float(holdout_metrics.get("net_return_sum") or 0.0) > 0.0,
                    "model_package": _path_text(model_path),
                    "threshold_report": _path_text(threshold_sweep_path) if threshold_sweep_path.exists() else "",
                    "eval_summary_path": _path_text(recipe_summary_path),
                    "training_report_path": _path_text(training_path),
                    "model_contract": "",
                    "exists": {
                        "model_package": model_path.exists(),
                        "threshold_report": threshold_sweep_path.exists(),
                        "eval_summary_path": recipe_summary_path.exists(),
                        "training_report_path": training_path.exists(),
                        "model_contract": False,
                    },
                    "ready_to_run": False,
                    "supports_terminal": False,
                    "research_url": f"/trading/research?{urlencode(query_values)}",
                    "missing_required": [],
                    "card_tone": "ready" if stage_a_passed else "warn",
                    "status_chip_class": "good" if stage_a_passed else "bad",
                    "status_label": "stage a pass" if stage_a_passed else "stage a fail",
                    "metrics": {
                        "stage_a_passed": stage_a_passed,
                        "side_share_in_band": side_share_in_band,
                        "profit_factor": profit_factor,
                        "net_return_sum": net_return_sum,
                        "trades": trades,
                        "win_rate": win_rate,
                        "long_share": long_share,
                        "threshold": recommended_threshold,
                    },
                    "metric_cards": [
                        _catalog_metric_card("Stage A", "PASS" if stage_a_passed else "FAIL"),
                        _catalog_metric_card("Side Balance", "PASS" if side_share_in_band else "FAIL"),
                        _catalog_metric_card("Trades", _format_catalog_int(trades)),
                        _catalog_metric_card("Win Rate", _format_catalog_percent(win_rate)),
                        _catalog_metric_card("Net Return", _format_catalog_percent(net_return_sum)),
                        _catalog_metric_card("Profit Factor", _format_catalog_number(profit_factor)),
                    ],
                    "path_rows": [
                        _catalog_path_row("Model Package", model_path),
                        _catalog_path_row("Recipe Summary", recipe_summary_path),
                        _catalog_path_row("Training Report", training_path),
                        _catalog_path_row("Threshold Sweep", threshold_sweep_path),
                    ],
                    "compatibility_note": "Use Research Explorer to replay this recovery model on any allowed out-of-sample range.",
                    "launch_url": "",
                    "prefill_url": "",
                    "evaluation_api_url": f"/api/trading/research/evaluation?{urlencode(query_values)}",
                    "feature_intelligence_api_url": "",
                    "actions": actions,
                    "_sort_group": 1 if stage_a_passed else 2,
                }
            )
    return entries


def _build_artifact_discovery_entries() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    entries.extend(_discover_published_model_roots(ML_PIPELINE_2_ARTIFACT_MODEL_CATALOG_DIR, source_label="artifact_discovery_ml_pipeline_2"))
    entries.extend(_build_recovery_catalog_entries())
    return entries


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
    model_contract_path = _resolve_repo_path(raw.get("model_contract"))

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
        "model_contract": bool(model_contract_path and model_contract_path.exists()),
    }
    missing_required: List[str] = []
    if not existence["model_package"]:
        missing_required.append("model_package")
    if not existence["threshold_report"]:
        missing_required.append("threshold_report")
    override_missing = raw.get("missing_required")
    if isinstance(override_missing, list):
        missing_required = [str(item).strip() for item in override_missing if str(item).strip()]

    ready_to_run = bool(raw.get("ready_to_run")) if "ready_to_run" in raw else len(missing_required) == 0
    supports_terminal = (
        bool(raw.get("supports_terminal"))
        if "supports_terminal" in raw
        else ready_to_run
    )
    research_url = str(raw.get("research_url") or "").strip()
    card_tone = str(raw.get("card_tone") or ("ready" if ready_to_run else "warn")).strip() or "warn"
    status_chip_class = str(raw.get("status_chip_class") or ("good" if ready_to_run else "bad")).strip() or "bad"
    status_label = str(raw.get("status_label") or ("ready" if ready_to_run else "needs setup")).strip() or "unknown"
    source_label = str(raw.get("source_label") or source).strip() or source

    query_values = {"model": instance_key}
    if model_path:
        query_values["model_package"] = _path_text(model_path)
    if threshold_path:
        query_values["threshold_report"] = _path_text(threshold_path)
    if summary_path:
        query_values["eval_summary_path"] = _path_text(summary_path)
    if training_path:
        query_values["training_report_path"] = _path_text(training_path)
    if model_contract_path:
        query_values["model_contract"] = _path_text(model_contract_path)
    prefill_url = f"/trading?{urlencode(query_values)}"

    eval_query: Dict[str, str] = {}
    if summary_path:
        eval_query["summary_path"] = _path_text(summary_path)
    if training_path:
        eval_query["training_report_path"] = _path_text(training_path)
    if threshold_path:
        eval_query["policy_report_path"] = _path_text(threshold_path)
    evaluation_api_url = f"/api/trading/model-evaluation?{urlencode(eval_query)}" if eval_query else ""
    feature_intelligence_api_url = f"/api/trading/feature-intelligence?model={quote(instance_key, safe='')}"
    metric_cards = [
        _catalog_metric_card(
            "Coverage",
            f"{training.get('coverage_start_date') or '--'} to {training.get('coverage_end_date') or '--'}",
        ),
        _catalog_metric_card("Full OOS Win %", _format_catalog_percent((full_oos or {}).get("win_rate"))),
        _catalog_metric_card("Latest Slice Win %", _format_catalog_percent((latest_oos or {}).get("win_rate"))),
        _catalog_metric_card("CE Threshold", _format_catalog_number(policy.get("ce_threshold"), digits=3)),
        _catalog_metric_card("PE Threshold", _format_catalog_number(policy.get("pe_threshold"), digits=3)),
        _catalog_metric_card("Policy Mode", str(policy.get("selection_mode") or "--")),
    ]
    path_rows = [
        _catalog_path_row("Model Package", model_path),
        _catalog_path_row("Threshold Report", threshold_path),
        _catalog_path_row("Eval Summary", summary_path),
        _catalog_path_row("Training Report", training_path),
    ]
    actions = _default_catalog_actions(
        prefill_url=prefill_url,
        launch_url=f"/trading/model/{instance_key}",
        evaluation_api_url=evaluation_api_url,
        supports_terminal=supports_terminal,
        research_url=research_url,
    )

    return {
        "catalog_kind": str(raw.get("catalog_kind") or "terminal"),
        "source": source,
        "source_label": source_label,
        "instance_key": instance_key,
        "profile_key": str(raw.get("profile_key") or ""),
        "model_group": str(raw.get("model_group") or ""),
        "profile_id": str(raw.get("profile_id") or ""),
        "run_id": str(raw.get("run_id") or ""),
        "feature_profile": str(raw.get("feature_profile") or ""),
        "title": str(raw.get("title") or instance_key),
        "summary": str(raw.get("summary") or ""),
        "description": str(raw.get("description") or ""),
        "recommended": bool(raw.get("recommended")),
        "model_package": _path_text(model_path),
        "threshold_report": _path_text(threshold_path),
        "eval_summary_path": _path_text(summary_path),
        "training_report_path": _path_text(training_path),
        "model_contract": _path_text(model_contract_path),
        "exists": existence,
        "ready_to_run": ready_to_run,
        "supports_terminal": supports_terminal,
        "research_url": research_url,
        "missing_required": missing_required,
        "card_tone": card_tone,
        "status_chip_class": status_chip_class,
        "status_label": status_label,
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
        "metric_cards": metric_cards,
        "path_rows": path_rows,
        "compatibility_note": eval_snapshot.get("runner_compatibility", {}).get("note"),
        "launch_url": f"/trading/model/{instance_key}",
        "prefill_url": prefill_url,
        "evaluation_api_url": evaluation_api_url,
        "feature_intelligence_api_url": feature_intelligence_api_url,
        "actions": actions,
        "_sort_group": int(raw.get("_sort_group")) if raw.get("_sort_group") is not None else (0 if ready_to_run else 2),
    }


def _build_trading_model_catalog() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()

    manifest_paths = sorted(TRADING_MODEL_CATALOG_DIR.glob("*/model.json")) if TRADING_MODEL_CATALOG_DIR.exists() else []
    for manifest in manifest_paths:
        payload = _safe_load_json(manifest)
        if not isinstance(payload, dict):
            continue
        required = ("instance_key", "model_package", "threshold_report")
        if any(not str(payload.get(k) or "").strip() for k in required):
            continue
        entry = _build_catalog_entry(payload, source="catalog_manifest", load_eval_snapshot=True)
        key = str(entry.get("instance_key") or "").strip().lower()
        if key and key not in seen_keys:
            entries.append(entry)
            seen_keys.add(key)

    for entry in _build_artifact_discovery_entries():
        key = str(entry.get("instance_key") or "").strip().lower()
        if key and key in seen_keys:
            continue
        entries.append(entry)
        if key:
            seen_keys.add(key)

    entries.sort(
        key=lambda item: (
            int(item.get("_sort_group", 2)),
            not bool(item.get("recommended")),
            str(item.get("source") or "").lower(),
            str(item.get("title") or "").lower(),
        )
    )
    return entries


def _resolve_trading_model_catalog_entry(model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    catalog = _build_trading_model_catalog()
    if not catalog:
        return None
    lookup = _normalize_trading_instance(model) if model else ""
    if lookup:
        for entry in catalog:
            if _normalize_trading_instance(entry.get("instance_key")) == lookup:
                return entry
            if _normalize_trading_instance(entry.get("profile_key")) == lookup:
                return entry
            if _normalize_trading_instance(entry.get("model_group")) == lookup:
                return entry
    for entry in catalog:
        if entry.get("ready_to_run"):
            return entry
    return catalog[0]


def _coerce_iso_day(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except Exception:
        return None


@lru_cache(maxsize=16)
def _load_model_package_cached(model_path_text: str) -> Optional[Dict[str, Any]]:
    if not model_path_text:
        return None
    try:
        import joblib  # type: ignore

        payload = joblib.load(model_path_text)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _extract_pipeline_feature_scores(model_obj: Any, base_feature_names: Sequence[str]) -> Tuple[List[str], List[Optional[float]]]:
    feature_names = [str(name).strip() for name in base_feature_names if str(name).strip()]
    estimator = model_obj
    if hasattr(model_obj, "named_steps"):
        named_steps = getattr(model_obj, "named_steps", {}) or {}
        estimator = named_steps.get("model") or (list(named_steps.values())[-1] if named_steps else model_obj)
        pre = named_steps.get("pre")
        if pre is not None and hasattr(pre, "get_feature_names_out"):
            try:
                transformed = [str(name).split("__")[-1].strip() for name in pre.get_feature_names_out()]
                if transformed:
                    feature_names = transformed
            except Exception:
                pass

    values: Optional[np.ndarray] = None
    if hasattr(estimator, "coef_"):
        try:
            coef = np.asarray(getattr(estimator, "coef_"), dtype=float)
            values = np.abs(coef)
            if values.ndim > 1:
                values = values.mean(axis=0)
        except Exception:
            values = None
    elif hasattr(estimator, "feature_importances_"):
        try:
            values = np.asarray(getattr(estimator, "feature_importances_"), dtype=float)
        except Exception:
            values = None

    if values is None:
        return feature_names, [None for _ in feature_names]

    flat = np.ravel(values)
    if len(flat) != len(feature_names) and len(flat) == len(base_feature_names):
        feature_names = [str(name).strip() for name in base_feature_names if str(name).strip()]
    if len(flat) != len(feature_names):
        limit = min(len(flat), len(feature_names))
        flat = flat[:limit]
        feature_names = feature_names[:limit]

    scores: List[Optional[float]] = []
    for value in flat.tolist():
        try:
            numeric = float(value)
        except Exception:
            numeric = float("nan")
        scores.append(numeric if math.isfinite(numeric) else None)
    return feature_names, scores


def _build_feature_intelligence_snapshot(
    model_entry: Dict[str, Any],
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    contract = _load_snapshot_ml_flat_dictionary()
    model_path = _resolve_repo_path(model_entry.get("model_package"))
    training_path = _resolve_repo_path(model_entry.get("training_report_path"))
    model_contract_path = _resolve_repo_path(model_entry.get("model_contract"))

    package = (
        _load_model_package_cached(str(model_path.resolve()))
        if isinstance(model_path, Path) and model_path.exists()
        else None
    )
    training_payload = _safe_load_json(training_path) if isinstance(training_path, Path) else None
    model_contract_payload = _safe_load_json(model_contract_path) if isinstance(model_contract_path, Path) else None

    base_feature_names = []
    if isinstance(package, dict):
        base_feature_names = [str(name).strip() for name in package.get("feature_columns", []) if str(name).strip()]
    if not base_feature_names and isinstance(model_contract_payload, dict):
        base_feature_names = [str(name).strip() for name in model_contract_payload.get("required_features", []) if str(name).strip()]

    side_models = package.get("models") if isinstance(package, dict) and isinstance(package.get("models"), dict) else {}
    side_keys = [str(key).strip() for key in side_models.keys() if str(key).strip()]
    direction_semantics = package.get("direction_semantics") if isinstance(package, dict) else {}

    raw_features: Dict[str, Dict[str, Any]] = {}
    for raw_name in base_feature_names:
        if raw_name not in raw_features:
            raw_features[raw_name] = {"side_scores": {}, "source_feature_count": 0}

    for side_key in side_keys:
        feature_names, scores = _extract_pipeline_feature_scores(side_models.get(side_key), base_feature_names)
        for raw_name, score in zip(feature_names, scores):
            row = raw_features.setdefault(raw_name, {"side_scores": {}, "source_feature_count": 0})
            row["source_feature_count"] = int(row.get("source_feature_count") or 0) + 1
            if score is not None:
                row["side_scores"][side_key] = score

    v1_features: Dict[str, Dict[str, Any]] = {}
    removed_legacy_count = 0
    unmapped_feature_count = 0
    rename_map = contract.get("rename_map", {})
    column_to_group = contract.get("column_to_group", {})
    removed_legacy = contract.get("removed_legacy", set())
    groups = contract.get("groups", {})
    field_labels = contract.get("field_labels", {})

    for raw_name, row in raw_features.items():
        translated_name = ""
        if raw_name in column_to_group:
            translated_name = raw_name
        elif raw_name in rename_map:
            translated_name = str(rename_map.get(raw_name) or "").strip()
        elif raw_name in removed_legacy:
            removed_legacy_count += 1
            continue
        else:
            unmapped_feature_count += 1
            continue

        group_key = str(column_to_group.get(translated_name) or "other")
        group_label = str((groups.get(group_key) or {}).get("label") or "Other")
        entry = v1_features.setdefault(
            translated_name,
            {
                "feature_name": translated_name,
                "feature_label": str(field_labels.get(translated_name) or translated_name),
                "group_key": group_key,
                "group_label": group_label,
                "side_scores": {},
                "source_feature_count": 0,
            },
        )
        entry["source_feature_count"] = int(entry.get("source_feature_count") or 0) + int(row.get("source_feature_count") or 1)
        for side_key, score in (row.get("side_scores") or {}).items():
            if score is None:
                continue
            existing = entry["side_scores"].get(side_key)
            entry["side_scores"][side_key] = max(existing, score) if existing is not None else score

    ranking_rows: List[Dict[str, Any]] = []
    for entry in v1_features.values():
        scores = [float(value) for value in (entry.get("side_scores") or {}).values() if value is not None]
        importance_score = float(np.mean(scores)) if scores else None
        ranking_rows.append(
            {
                **entry,
                "importance_score": importance_score,
            }
        )
    ranking_rows.sort(
        key=lambda row: (
            row.get("importance_score") is None,
            -float(row.get("importance_score") or 0.0),
            str(row.get("feature_name") or ""),
        )
    )
    for idx, row in enumerate(ranking_rows, start=1):
        row["rank"] = idx

    ranking_by_name = {str(row.get("feature_name")): row for row in ranking_rows}

    grouped_rows: List[Dict[str, Any]] = []
    for group_key in contract.get("group_order", []):
        payload = groups.get(group_key) or {}
        columns = [str(name).strip() for name in payload.get("columns", []) if str(name).strip()]
        features: List[Dict[str, Any]] = []
        selected_count = 0
        importance_values: List[float] = []
        for column in columns:
            selected = ranking_by_name.get(column)
            if selected is not None:
                selected_count += 1
                if selected.get("importance_score") is not None:
                    importance_values.append(float(selected["importance_score"]))
            features.append(
                {
                    "feature_name": column,
                    "feature_label": str(field_labels.get(column) or column),
                    "group_key": group_key,
                    "group_label": str(payload.get("label") or group_key),
                    "is_selected": bool(selected),
                    "importance_score": selected.get("importance_score") if selected else None,
                    "rank": selected.get("rank") if selected else None,
                    "side_scores": selected.get("side_scores") if selected else {},
                }
            )
        grouped_rows.append(
            {
                "group_key": group_key,
                "group_label": str(payload.get("label") or group_key),
                "contract_columns_total": len(columns),
                "selected_columns_count": selected_count,
                "inactive_columns_count": len(columns) - selected_count,
                "importance_mean": float(np.mean(importance_values)) if importance_values else None,
                "features": features,
            }
        )

    axis_keys = side_keys[:2]
    axis_labels: List[str] = []
    for side_key in axis_keys:
        label_value = None
        if isinstance(direction_semantics, dict):
            label_value = direction_semantics.get(side_key)
            if isinstance(label_value, dict):
                label_value = label_value.get("label") or label_value.get("name") or label_value.get("side")
        pretty = str(label_value or side_key).strip().upper() or side_key.upper()
        axis_labels.append(f"{pretty} importance")

    scatter_points: List[Dict[str, Any]] = []
    for row in ranking_rows:
        side_scores = row.get("side_scores") or {}
        if axis_keys:
            x_value = side_scores.get(axis_keys[0])
            y_value = side_scores.get(axis_keys[1]) if len(axis_keys) > 1 else row.get("importance_score")
        else:
            x_value = row.get("importance_score")
            y_value = row.get("importance_score")
        if x_value is None or y_value is None:
            continue
        scatter_points.append(
            {
                "feature_name": row.get("feature_name"),
                "feature_label": row.get("feature_label"),
                "group_key": row.get("group_key"),
                "group_label": row.get("group_label"),
                "x": x_value,
                "y": y_value,
                "importance_score": row.get("importance_score"),
                "rank": row.get("rank"),
            }
        )

    coverage_start, coverage_end, coverage_days = _extract_training_coverage_range(training_payload)
    requested_from = _coerce_iso_day(date_from)
    requested_to = _coerce_iso_day(date_to)
    coverage_match: Optional[bool] = None
    if coverage_start and coverage_end and requested_from and requested_to:
        coverage_match = coverage_start <= requested_from and requested_to <= coverage_end

    selected_model = package.get("selected_model") if isinstance(package, dict) and isinstance(package.get("selected_model"), dict) else {}
    return {
        "model": {
            "instance_key": str(model_entry.get("instance_key") or ""),
            "title": str(model_entry.get("title") or model_entry.get("instance_key") or "model"),
            "source": str(model_entry.get("source") or ""),
            "model_group": str(model_entry.get("model_group") or ""),
            "profile_id": str(model_entry.get("profile_id") or ""),
            "run_id": str(model_entry.get("run_id") or ""),
            "feature_profile": str(model_entry.get("feature_profile") or package.get("feature_profile") or ""),
            "selected_feature_set": str(package.get("selected_feature_set") or ""),
            "selected_model_name": str(selected_model.get("name") or ""),
            "selected_model_family": str(selected_model.get("family") or ""),
            "coverage": {
                "training_start": coverage_start,
                "training_end": coverage_end,
                "training_days": coverage_days,
                "requested_start": requested_from,
                "requested_end": requested_to,
                "requested_range_in_coverage": coverage_match,
            },
        },
        "contract": {
            "contract_id": str(contract.get("contract_id") or "snapshot_ml_flat"),
            "schema_version": str(contract.get("schema_version") or "unknown"),
            "groups": [
                {
                    "group_key": group_key,
                    "group_label": str((groups.get(group_key) or {}).get("label") or group_key),
                    "contract_columns_total": len((groups.get(group_key) or {}).get("columns") or []),
                }
                for group_key in contract.get("group_order", [])
            ],
        },
        "summary": {
            "selected_v1_feature_count": len(ranking_rows),
            "contract_group_count": len(grouped_rows),
            "removed_legacy_feature_count": removed_legacy_count,
            "unmapped_feature_count": unmapped_feature_count,
            "scatter_point_count": len(scatter_points),
            "requested_range_in_coverage": coverage_match,
        },
        "ranking": {
            "rows": ranking_rows,
        },
        "groups": grouped_rows,
        "scatter": {
            "x_axis_key": axis_keys[0] if axis_keys else "importance_score",
            "x_axis_label": axis_labels[0] if axis_labels else "Importance",
            "y_axis_key": axis_keys[1] if len(axis_keys) > 1 else "importance_score",
            "y_axis_label": axis_labels[1] if len(axis_labels) > 1 else "Importance",
            "points": scatter_points,
        },
        "files": {
            "model_package": _path_text(model_path),
            "training_report": _path_text(training_path),
            "model_contract": _path_text(model_contract_path),
        },
    }


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
        pass
    try:
        r = _redis_sync_client()
        virtual_time_enabled = str(r.get("system:virtual_time:enabled") or "").strip().lower() in {"1", "true", "yes", "on"}
        historical_ready = str(r.get("system:historical:data_ready") or "").strip().lower() in {"1", "true", "yes", "on"}
        replay_status_raw = r.get("system:historical:replay_status")
        replay_status = {}
        if isinstance(replay_status_raw, str) and replay_status_raw.strip():
            try:
                loaded = json.loads(replay_status_raw)
                if isinstance(loaded, dict):
                    replay_status = loaded
            except Exception:
                replay_status = {}
        replay_mode = str(replay_status.get("mode") or "").strip().lower()
        replay_status_text = str(replay_status.get("status") or "").strip().lower()
        if replay_mode == "historical":
            return "historical"
        if historical_ready or virtual_time_enabled or replay_status_text in {"ready", "running", "complete", "completed"}:
            return "historical"
    except Exception:
        pass
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

    now = datetime.now(IST_ZONE)
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
    now_iso = _now_iso_ist()

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
    now_iso = _now_iso_ist()

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

    # Strategy evaluation run progress
    if destination.startswith("/topic/strategy/eval/run/"):
        run_id = destination.split("/", 5)[-1]
        if run_id:
            return [("channel", f"strategy:eval:run:{run_id}")]

    if destination == "/topic/strategy/eval/global":
        return [("channel", "strategy:eval:global")]

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
                "timestamp": _now_iso_ist(),
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


def _now_iso_ist() -> str:
    if isoformat_ist is not None:
        return isoformat_ist()
    return datetime.now(IST_ZONE).isoformat()


_operator_routes = DashboardOperatorRouter(
    templates=templates,
    templates_dir=templates_dir,
    market_data_api_url=MARKET_DATA_API_URL,
    redis_host=REDIS_HOST,
    redis_port=REDIS_PORT,
    get_live_strategy_monitor_service=lambda: _live_strategy_monitor_service,
    get_strategy_eval_service=lambda: _strategy_eval_service,
    normalize_timestamp_fields=_normalize_timestamp_fields,
    now_iso_ist=_now_iso_ist,
)
app.include_router(_operator_routes.router)

_historical_replay_routes = DashboardHistoricalReplayRouter(
    templates=templates,
    templates_dir=templates_dir,
    get_historical_replay_service=lambda: _historical_replay_monitor_service,
    now_iso_ist=_now_iso_ist,
)
app.include_router(_historical_replay_routes.router)

_strategy_evaluation_routes = DashboardStrategyEvaluationRouter(
    templates=templates,
    get_strategy_eval_service=lambda: _strategy_eval_service,
    normalize_timestamp_fields=_normalize_timestamp_fields,
)
app.include_router(_strategy_evaluation_routes.router)

_model_catalog_routes = DashboardModelCatalogRouter(
    templates=templates,
    build_trading_model_catalog=lambda: _build_trading_model_catalog(),
    legacy_trading_runtime_status=lambda: _legacy_trading_runtime_status(),
    normalize_trading_instance=lambda value: _normalize_trading_instance(value),
    resolve_trading_model_catalog_entry=lambda model: _resolve_trading_model_catalog_entry(model),
    resolve_repo_path=lambda value, default=None: _resolve_repo_path(value, default),
    build_model_eval_snapshot=lambda summary_file, training_file, policy_file: _build_model_eval_snapshot(
        summary_file,
        training_file,
        policy_file,
    ),
    build_feature_intelligence_snapshot=lambda model_entry, date_from=None, date_to=None: _build_feature_intelligence_snapshot(
        model_entry,
        date_from=date_from,
        date_to=date_to,
    ),
    default_model_eval_summary_path=DEFAULT_MODEL_EVAL_SUMMARY_PATH,
    default_model_training_report_path=DEFAULT_MODEL_TRAINING_REPORT_PATH,
    default_model_policy_report_path=DEFAULT_MODEL_POLICY_REPORT_PATH,
)
app.include_router(_model_catalog_routes.router)

_research_routes = DashboardResearchRouter(
    templates=templates,
    list_recovery_scenarios_fn=lambda: _list_recovery_scenarios_for_dashboard(),
    evaluate_recovery_scenario_fn=lambda **kwargs: _evaluate_recovery_scenario_for_dashboard(**kwargs),
    research_eval_available=lambda: evaluate_recovery_scenario is not None and list_recovery_scenarios is not None,
)
app.include_router(_research_routes.router)

async def _unbound_public_contract_market_data_handler(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("public contract market-data handlers are not bound")


def _require_debug_routes_enabled() -> None:
    enabled = str(os.getenv("DASHBOARD_ENABLE_DEBUG_ROUTES") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        raise HTTPException(
            status_code=404,
            detail="debug routes are disabled; set DASHBOARD_ENABLE_DEBUG_ROUTES=1 to enable",
        )

_public_contract_routes = DashboardPublicContractRouter(
    now_iso_ist=_now_iso_ist,
    normalize_timestamp_fields=_normalize_timestamp_fields,
    public_topic_schemas=lambda: _public_topic_schemas(),
    public_schema_version=PUBLIC_SCHEMA_VERSION,
    public_topics=PUBLIC_TOPICS,
    build_runtime_catalog=lambda instrument=None: _build_runtime_catalog(instrument=instrument),
    public_timeframes=PUBLIC_TIMEFRAMES,
    load_runtime_instruments=lambda max_instruments=20: _load_runtime_instruments(max_instruments=max_instruments),
    default_instrument=DEFAULT_INSTRUMENT,
    canonical_contract_timeframe=lambda timeframe: _canonical_contract_timeframe(timeframe),
    get_system_mode=_operator_routes.get_system_mode,
    market_data_api_url=MARKET_DATA_API_URL,
    requests_get=lambda url, timeout=3: requests.get(url, timeout=timeout),
    get_ohlc_data=_unbound_public_contract_market_data_handler,
    get_technical_indicators=_unbound_public_contract_market_data_handler,
    get_market_depth=_unbound_public_contract_market_data_handler,
    get_options_chain=_unbound_public_contract_market_data_handler,
    get_current_mode_hint=lambda timeout_seconds=1.0: _get_current_mode_hint(timeout_seconds=timeout_seconds),
)
app.include_router(_public_contract_routes.router)

_debug_routes = DashboardDebugRouter(
    base_dir=Path(__file__).parent,
    require_debug_routes_enabled=_require_debug_routes_enabled,
    redis_host=REDIS_HOST,
    redis_port=REDIS_PORT,
    default_instrument=DEFAULT_INSTRUMENT,
    logger=logger,
)
app.include_router(_debug_routes.router)

_legacy_trading_routes = DashboardLegacyTradingRouter(
    templates=templates,
    repo_root=REPO_ROOT,
    ml_pipeline_src=ML_PIPELINE_SRC,
    default_instrument=DEFAULT_INSTRUMENT,
    redis_host=REDIS_HOST,
    redis_port=REDIS_PORT,
    default_trading_events_path=DEFAULT_TRADING_EVENTS_PATH,
    default_model_package="ml_pipeline/artifacts/models/by_features/core_v2/h5_ts0_lgbm_regime/model/model.joblib",
    default_threshold_report="ml_pipeline/artifacts/models/by_features/core_v2/h5_ts0_lgbm_regime/config/profiles/openfe_v9_dual/threshold_report.json",
    logger=logger,
    legacy_trading_runtime_status=lambda: _legacy_trading_runtime_status(),
    build_trading_model_catalog=lambda: _build_trading_model_catalog(),
    normalize_trading_instance=lambda value: _normalize_trading_instance(value),
    resolve_repo_path=lambda value, default=None: _resolve_repo_path(value, default),
    coerce_float=lambda value: _coerce_float(value),
    truthy=lambda value, default=False: _truthy(value, default=default),
    now_ist=lambda: datetime.now(IST_TZ),
    json_safe_value=lambda value: _json_safe_value(value),
    save_latest_backtest_state=lambda instance, payload: _save_latest_backtest_state(instance, payload),
    load_latest_backtest_state=lambda instance: _load_latest_backtest_state(instance),
    trading_lock=_TRADING_LOCK,
    default_trading_paths=lambda instance: _default_trading_paths(instance),
    refresh_trading_runner_state=lambda instance: _refresh_trading_runner_state(instance),
    stop_trading_process_locked=lambda state, reason="manual_stop": _stop_trading_process_locked(state, reason=reason),
    close_trading_log_handles=lambda state: _close_trading_log_handles(state),
    load_runtime_instruments=lambda max_instruments=50: _load_runtime_instruments(max_instruments=max_instruments),
    select_most_active_instrument=lambda instruments, preferred_mode="live": _select_most_active_instrument(
        instruments,
        preferred_mode=preferred_mode,
    ),
    is_placeholder_instrument=lambda value: _is_placeholder_instrument(value),
    load_trading_events=lambda path, limit=None: _load_trading_events(path, limit=limit),
    build_trading_state=lambda events: _build_trading_state(events),
    backtest_timeout_seconds=int(os.getenv("DASHBOARD_LEGACY_BACKTEST_TIMEOUT_SECONDS") or "1800"),
)
app.include_router(_legacy_trading_routes.router)

# Backward-compatible callables used by local tests/imports.
home = _operator_routes.home
live_strategy = _operator_routes.live_strategy
get_live_strategy_session = _operator_routes.get_live_strategy_session
get_live_strategy_traces = _operator_routes.get_live_strategy_traces
get_live_strategy_trace_detail = _operator_routes.get_live_strategy_trace_detail
health = _operator_routes.health
market_data_health = _operator_routes.market_data_health
get_system_mode = _operator_routes.get_system_mode
historical_replay = _historical_replay_routes.historical_replay
get_historical_strategy_session = _historical_replay_routes.get_historical_strategy_session
get_historical_replay_status = _historical_replay_routes.get_historical_replay_status
replay_health = _historical_replay_routes.replay_health
strategy_evaluation_page = _strategy_evaluation_routes.strategy_evaluation_page
get_strategy_evaluation_summary = _strategy_evaluation_routes.get_strategy_evaluation_summary
get_strategy_evaluation_equity = _strategy_evaluation_routes.get_strategy_evaluation_equity
get_strategy_evaluation_days = _strategy_evaluation_routes.get_strategy_evaluation_days
get_strategy_evaluation_trades = _strategy_evaluation_routes.get_strategy_evaluation_trades
create_strategy_evaluation_run = _strategy_evaluation_routes.create_strategy_evaluation_run
get_latest_strategy_evaluation_run = _strategy_evaluation_routes.get_latest_strategy_evaluation_run
get_strategy_evaluation_run = _strategy_evaluation_routes.get_strategy_evaluation_run
trading_models_page = _model_catalog_routes.trading_models_page
get_trading_models = _model_catalog_routes.get_trading_models
trading_terminal_model = _model_catalog_routes.trading_terminal_model
get_trading_model_evaluation = _model_catalog_routes.get_trading_model_evaluation
get_trading_feature_intelligence = _model_catalog_routes.get_trading_feature_intelligence
trading_research_page = _research_routes.trading_research_page
get_trading_research_scenarios = _research_routes.get_trading_research_scenarios
get_trading_research_evaluation = _research_routes.get_trading_research_evaluation
get_public_schema_index = _public_contract_routes.get_public_schema_index
get_public_topic_schema = _public_contract_routes.get_public_topic_schema
get_public_capabilities = _public_contract_routes.get_public_capabilities
get_public_catalog = _public_contract_routes.get_public_catalog
get_public_topic_example = _public_contract_routes.get_public_topic_example
test_page = _debug_routes.test_page
test_redis = _debug_routes.test_redis
test_ltp = _debug_routes.test_ltp
test_ohlc = _debug_routes.test_ohlc
simple_dashboard = _debug_routes.simple_dashboard
simple_ohlc = _debug_routes.simple_ohlc
simple_ltp = _debug_routes.simple_ltp
simple_redis_stats = _debug_routes.simple_redis_stats
trading_terminal = _legacy_trading_routes.trading_terminal
run_trading_backtest = _legacy_trading_routes.run_trading_backtest
get_latest_backtest_state = _legacy_trading_routes.get_latest_backtest_state
get_trading_state = _legacy_trading_routes.get_trading_state
start_trading_runner = _legacy_trading_routes.start_trading_runner
stop_trading_runner = _legacy_trading_routes.stop_trading_runner


def _default_trading_paths(instance: str) -> Tuple[Path, Path, Path]:
    key = _normalize_trading_instance(instance)
    if key == _TRADING_DEFAULT_INSTANCE:
        return DEFAULT_TRADING_EVENTS_PATH, DEFAULT_TRADING_STDOUT_PATH, DEFAULT_TRADING_STDERR_PATH
    artifacts_dir = _LEGACY_TRADING_ARTIFACTS_DIR
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
        "strategy_eval": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Strategy Evaluation Run Event",
            "type": "object",
            "required": ["event_type", "run_id", "timestamp"],
            "properties": {
                "event_type": {
                    "type": "string",
                    "enum": [
                        "run_queued",
                        "run_started",
                        "run_progress",
                        "run_completed",
                        "run_failed",
                        "evaluation_ready",
                    ],
                },
                "run_id": {"type": "string"},
                "timestamp": {"type": "string", "format": "date-time"},
                "progress_pct": {"type": ["number", "null"]},
                "current_day": {"type": ["string", "null"]},
                "total_days": {"type": ["integer", "number", "null"]},
                "message": {"type": ["string", "null"]},
                "error": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
    }


async def _build_runtime_catalog(instrument: Optional[str] = None) -> Dict[str, Any]:
    now_iso = _now_iso_ist()
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
            "strategy_eval_global": "/topic/strategy/eval/global",
            "strategy_eval_run_template": "/topic/strategy/eval/run/{run_id}",
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


async def get_redis_mongo_sync_lag(instrument: str = ""):
    """Report Redis vs Mongo lag for live read-model domains."""
    selected_instrument = _normalize_instrument_symbol(instrument) or DEFAULT_INSTRUMENT
    if not selected_instrument:
        discovered = await asyncio.to_thread(_discover_instruments_from_redis, 1)
        selected_instrument = discovered[0] if discovered else ""
    if not selected_instrument:
        return {
            "status": "no_instrument",
            "error": "No instrument provided and no redis-discovered instrument available",
            "timestamp": _now_iso_ist(),
            "checks": {},
        }

    mode_hint = _get_current_mode_hint(timeout_seconds=1.0)
    thresholds = {
        "snapshot": int(os.getenv("SYNC_LAG_THRESHOLD_SNAPSHOT_SECONDS") or "120"),
        "tick": int(os.getenv("SYNC_LAG_THRESHOLD_TICK_SECONDS") or "30"),
        "depth": int(os.getenv("SYNC_LAG_THRESHOLD_DEPTH_SECONDS") or "60"),
        "options": int(os.getenv("SYNC_LAG_THRESHOLD_OPTIONS_SECONDS") or "120"),
    }

    redis_ok = False
    redis_error: Optional[str] = None
    r: Optional[redis.Redis] = None
    try:
        r = _redis_sync_client()
        r.ping()
        redis_ok = True
    except Exception as exc:
        redis_error = str(exc)

    checks: Dict[str, Any] = {}

    # Snapshot lag: compare latest Redis OHLC minute timestamp (proxy for live stream)
    # with latest persisted snapshot timestamp in Mongo.
    redis_snapshot_ts = None
    redis_snapshot_source = None
    if redis_ok and r is not None:
        bars, redis_ohlc_key = await asyncio.to_thread(
            _read_ohlc_from_redis,
            selected_instrument,
            "1min",
            1,
            "desc",
            mode_hint,
            bool(mode_hint),
        )
        if bars:
            latest_bar = bars[-1]
            redis_snapshot_ts = latest_bar.get("start_at") or latest_bar.get("timestamp")
            redis_snapshot_source = redis_ohlc_key or "ohlc_sorted_proxy"
    mongo_snapshot = await asyncio.to_thread(_load_latest_snapshot_from_mongo, selected_instrument)
    snapshot_doc_ts = (mongo_snapshot or {}).get("snapshot_timestamp")
    checks["snapshot"] = _lag_check_payload(
        name="snapshot",
        redis_timestamp=redis_snapshot_ts,
        mongo_timestamp=snapshot_doc_ts,
        threshold_seconds=thresholds["snapshot"],
        redis_source=redis_snapshot_source or "redis_ohlc_proxy",
        mongo_source=str(os.getenv("MONGO_COLL_SNAPSHOTS") or "phase1_market_snapshots"),
        note="Redis side uses latest 1m OHLC timestamp as snapshot proxy (snapshot events are pub/sub).",
    )

    # Tick lag
    redis_tick_ts = None
    redis_tick_source = None
    if redis_ok and r is not None:
        tick_keys = _redis_prefixed_keys(
            mode_hint,
            [
                f"websocket:tick:{selected_instrument}:latest",
                f"tick:{selected_instrument}:latest",
                f"tick:{selected_instrument}",
            ],
        )
        tick_key, tick_raw = _redis_get_first_value(r, tick_keys)
        redis_tick_source = tick_key
        tick_obj = _safe_json_loads(tick_raw) if tick_raw else None
        if isinstance(tick_obj, dict):
            redis_tick_ts = tick_obj.get("timestamp") or tick_obj.get("exchange_timestamp")
    mongo_tick_info = await asyncio.to_thread(
        _mongo_latest_ts_for_instrument,
        str(os.getenv("MONGO_COLL_TICKS") or "live_ticks").strip() or "live_ticks",
        selected_instrument,
    )
    checks["tick"] = _lag_check_payload(
        name="tick",
        redis_timestamp=redis_tick_ts,
        mongo_timestamp=mongo_tick_info.get("timestamp"),
        threshold_seconds=thresholds["tick"],
        redis_source=redis_tick_source,
        mongo_source=mongo_tick_info.get("collection"),
        note=(
            "Mongo tick persistence appears disabled or not wired."
            if not mongo_tick_info.get("collection_exists")
            else None
        ),
    )

    # Depth lag
    redis_depth_ts = None
    redis_depth_source = None
    if redis_ok and r is not None:
        depth_keys = _redis_prefixed_keys(mode_hint, [f"depth:{selected_instrument}:timestamp"])
        depth_key, depth_raw = _redis_get_first_value(r, depth_keys)
        redis_depth_source = depth_key
        redis_depth_ts = depth_raw
    mongo_depth_info = await asyncio.to_thread(
        _mongo_latest_ts_for_instrument,
        str(os.getenv("MONGO_COLL_DEPTH") or "live_depth").strip() or "live_depth",
        selected_instrument,
    )
    checks["depth"] = _lag_check_payload(
        name="depth",
        redis_timestamp=redis_depth_ts,
        mongo_timestamp=mongo_depth_info.get("timestamp"),
        threshold_seconds=thresholds["depth"],
        redis_source=redis_depth_source,
        mongo_source=mongo_depth_info.get("collection"),
        note=(
            "Mongo depth persistence appears disabled or not wired."
            if not mongo_depth_info.get("collection_exists")
            else None
        ),
    )

    # Options lag
    redis_options_ts = None
    redis_options_source = None
    if redis_ok and r is not None:
        options_keys = _redis_prefixed_keys(
            mode_hint,
            [f"options:{selected_instrument}:chain"],
        )
        opt_key, opt_raw = _redis_get_first_value(r, options_keys)
        if opt_raw is None:
            for mode in _mode_priority(mode_hint):
                scan_matches = _scan_keys_limited(
                    r,
                    f"{mode}:options:{selected_instrument}:*:chain",
                    max_keys=3,
                    max_pages=4,
                )
                if scan_matches:
                    candidate_key = scan_matches[0]
                    try:
                        candidate_raw = r.get(candidate_key)
                    except Exception:
                        candidate_raw = None
                    if candidate_raw is not None:
                        opt_key = candidate_key
                        opt_raw = candidate_raw
                        break
        redis_options_source = opt_key
        opt_obj = _safe_json_loads(opt_raw) if opt_raw else None
        if isinstance(opt_obj, dict):
            redis_options_ts = opt_obj.get("timestamp")
    mongo_options_info = await asyncio.to_thread(
        _mongo_latest_ts_for_instrument,
        str(os.getenv("MONGO_COLL_OPTIONS") or "live_options_chain").strip() or "live_options_chain",
        selected_instrument,
    )
    checks["options"] = _lag_check_payload(
        name="options",
        redis_timestamp=redis_options_ts,
        mongo_timestamp=mongo_options_info.get("timestamp"),
        threshold_seconds=thresholds["options"],
        redis_source=redis_options_source,
        mongo_source=mongo_options_info.get("collection"),
        note=(
            "Mongo options persistence appears disabled or not wired."
            if not mongo_options_info.get("collection_exists")
            else None
        ),
    )

    summary_counts = {
        "ok": 0,
        "lagging": 0,
        "mongo_missing": 0,
        "redis_missing": 0,
        "no_data": 0,
    }
    for item in checks.values():
        s = str((item or {}).get("status") or "").lower()
        if s in summary_counts:
            summary_counts[s] += 1

    overall_status = "ok"
    if summary_counts["lagging"] > 0:
        overall_status = "lagging"
    elif (summary_counts["mongo_missing"] + summary_counts["redis_missing"] + summary_counts["no_data"]) > 0:
        overall_status = "partial"

    return _normalize_timestamp_fields(
        {
            "status": overall_status,
            "timestamp": _now_iso_ist(),
            "mode_hint": mode_hint or "unknown",
            "instrument": selected_instrument,
            "redis": {
                "ok": redis_ok,
                "error": redis_error,
                "host": REDIS_HOST,
                "port": REDIS_PORT,
            },
            "checks": checks,
            "summary": summary_counts,
            "architecture_note": "Keep split: Redis for low-latency market reads, Mongo for durable snapshot/strategy truth.",
        }
    )


async def market_data_status():
    """Get comprehensive market data status"""
    status = {
        "timestamp": _now_iso_ist(),
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
    default_instruments: List[Dict[str, Optional[str]]] = []
    if DEFAULT_INSTRUMENT:
        normalized_default = _normalize_instrument_entry(DEFAULT_INSTRUMENT)
        if normalized_default is not None:
            default_instruments.append(normalized_default)
    instrument_specs: List[Dict[str, Optional[str]]] = default_instruments[:]
    try:
        resp = requests.get(f"{MARKET_DATA_API_URL}/api/v1/market/instruments", timeout=2)
        if resp.status_code == 200:
            api_instruments = resp.json()
            if isinstance(api_instruments, dict) and "instruments" in api_instruments:
                api_instruments = api_instruments["instruments"]
            if isinstance(api_instruments, list) and api_instruments:
                normalized_api_instruments: List[Dict[str, Optional[str]]] = []
                for item in api_instruments:
                    normalized = _normalize_instrument_entry(item)
                    if normalized is not None:
                        normalized_api_instruments.append(normalized)
                if normalized_api_instruments:
                    instrument_specs = normalized_api_instruments
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
    if r is not None and (not instrument_specs or instrument_specs == default_instruments):
        try:
            discovered = await asyncio.to_thread(_discover_instruments_from_redis, 25)
            if discovered:
                normalized_discovered: List[Dict[str, Optional[str]]] = []
                for item in discovered:
                    normalized = _normalize_instrument_entry(item)
                    if normalized is not None:
                        normalized_discovered.append(normalized)
                if normalized_discovered:
                    instrument_specs = normalized_discovered
        except Exception:
            pass

    api_mode = str(status.get("market_data_api", {}).get("mode") or "").strip().lower()
    if api_mode not in {"live", "historical", "paper"}:
        api_mode = None

    for spec in instrument_specs:
        symbol = str(spec.get("symbol") or "").strip()
        exchange = str(spec.get("exchange") or "").strip().upper() or None
        if not symbol:
            continue
        try:
            if not r:
                status["instruments"][symbol] = {
                    "symbol": symbol,
                    "exchange": exchange,
                    "status": "unreachable",
                    "error": "Redis unavailable",
                }
                continue

            # Prefer keys from current execution mode, then fall back to any mode.
            # This avoids showing historical namespace as green/available during live runs.
            best_key = None
            best_count = 0
            best_mode_key = None
            best_mode_count = 0

            for key in _ohlc_sorted_keys_to_try(
                symbol,
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
                api_fallback = await asyncio.to_thread(_fetch_api_instrument_fallback, symbol)
                if api_fallback is not None:
                    status["instruments"][symbol] = {
                        "symbol": symbol,
                        "exchange": exchange,
                        **api_fallback,
                    }
                    continue

                status["instruments"][symbol] = {
                    "symbol": symbol,
                    "exchange": exchange,
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

            status["instruments"][symbol] = {
                "symbol": symbol,
                "exchange": exchange,
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
            status["instruments"][symbol] = {
                "symbol": symbol,
                "exchange": exchange,
                "status": "error",
                "error": str(e),
            }

    # Data validation checks
    status["data_validation"] = validate_data_availability(status)
    status["status"] = str(status["data_validation"].get("overall_status") or "unknown")

    return _normalize_timestamp_fields(status)


def _strategy_runtime_metrics_tail_limit() -> int:
    raw_value = os.getenv("DASHBOARD_STRATEGY_RUNTIME_METRICS_TAIL", "25")
    try:
        return max(1, int(raw_value))
    except Exception:
        return 25


async def _load_strategy_runtime_observability() -> Dict[str, Any]:
    if load_strategy_runtime_observability is None:
        return {
            "status": "unavailable",
            "checked_at_ist": _now_iso_ist(),
            "service": "market-data-dashboard",
            "error": "strategy runtime observability helper unavailable",
        }

    try:
        payload = await asyncio.to_thread(
            load_strategy_runtime_observability,
            repo_root=REPO_ROOT,
            metrics_tail_limit=_strategy_runtime_metrics_tail_limit(),
        )
        if isinstance(payload, dict):
            return _normalize_timestamp_fields(payload)
        return {
            "status": "error",
            "checked_at_ist": _now_iso_ist(),
            "service": "market-data-dashboard",
            "error": "strategy runtime observability returned non-object payload",
        }
    except Exception as exc:
        return {
            "status": "error",
            "checked_at_ist": _now_iso_ist(),
            "service": "market-data-dashboard",
            "error": str(exc),
        }


@app.get("/api/health/strategy-runtime")
async def strategy_runtime_health():
    """Operator-facing view of strategy runtime artifacts published under .run."""
    return await _load_strategy_runtime_observability()


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
        payload["timestamp"] = _now_iso_ist()
        payload["status"] = "ok"
        return _normalize_timestamp_fields(payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error building chart data for %s %s", instrument, timeframe)
        raise HTTPException(status_code=500, detail=str(e))

async def get_technical_indicators(instrument: str, timeframe: str = "1min"):
    """Get technical indicators from persisted snapshot source (Mongo)."""
    tf_canonical = _canonical_indicator_timeframe(timeframe)
    threshold = _indicator_stale_threshold_seconds(tf_canonical)
    try:
        selected_snapshot = await asyncio.to_thread(_load_latest_snapshot_from_mongo, instrument)
        snapshot = (
            selected_snapshot.get("snapshot")
            if isinstance(selected_snapshot, dict) and isinstance(selected_snapshot.get("snapshot"), dict)
            else {}
        )
        if not snapshot:
            return {
                "instrument": instrument,
                "timeframe": tf_canonical,
                "indicators": {},
                "status": "no_data",
                "error": "No persisted snapshot found for instrument",
                "indicator_timestamp": None,
                "indicator_source": "mongo_snapshots",
                "indicator_stream": "Y2",
                "indicator_update_type": "snapshot_event",
                "indicator_age_seconds": None,
                "indicator_is_stale": True,
                "indicator_stale_threshold_seconds": threshold,
                "bars_available": 0,
                "warmup_requirements": {},
                "timestamp": _now_iso_ist(),
            }

        indicators = _snapshot_to_indicator_fields(snapshot)
        if not indicators:
            return {
                "instrument": instrument,
                "timeframe": tf_canonical,
                "indicators": {},
                "status": "no_data",
                "error": "Latest snapshot missing indicator fields (mtf_derived/futures_derived)",
                "indicator_timestamp": _extract_snapshot_timestamp(
                    snapshot,
                    fallback_ts=(selected_snapshot or {}).get("snapshot_timestamp"),
                ),
                "indicator_source": "mongo_snapshots",
                "indicator_stream": "Y2",
                "indicator_update_type": "snapshot_event",
                "indicator_age_seconds": None,
                "indicator_is_stale": True,
                "indicator_stale_threshold_seconds": threshold,
                "bars_available": 0,
                "warmup_requirements": {},
                "timestamp": _now_iso_ist(),
            }

        indicator_timestamp = _extract_snapshot_timestamp(
            snapshot,
            fallback_ts=(selected_snapshot or {}).get("snapshot_timestamp"),
        ) or _now_iso_ist()
        parsed_ts = _parse_timestamp_flexible(indicator_timestamp)
        age_seconds: Optional[float] = None
        is_stale = False
        if parsed_ts is not None:
            age_seconds = max(0.0, (datetime.now(tz=IST_ZONE) - parsed_ts).total_seconds())
            is_stale = age_seconds > float(threshold)

        return _normalize_timestamp_fields(
            {
                "instrument": instrument,
                "timeframe": tf_canonical,
                "indicators": indicators,
                "status": "stale" if is_stale else "ok",
                "timestamp": _now_iso_ist(),
                "market_timestamp": indicator_timestamp,
                "indicator_timestamp": indicator_timestamp,
                "indicator_source": "mongo_snapshots",
                "indicator_stream": "Y2",
                "indicator_update_type": "snapshot_event",
                "indicator_age_seconds": age_seconds,
                "indicator_is_stale": is_stale,
                "indicator_stale_threshold_seconds": threshold,
                "bars_available": 0,
                "warmup_requirements": {},
            }
        )
    except Exception as e:
        logger.exception("Failed to load indicators from mongo snapshots for %s", instrument)
        return {
            "instrument": instrument,
            "timeframe": tf_canonical,
            "indicators": {},
            "status": "error",
            "error": str(e),
            "indicator_timestamp": None,
            "indicator_source": "mongo_snapshots",
            "indicator_stream": "Y2",
            "indicator_update_type": "snapshot_event",
            "indicator_age_seconds": None,
            "indicator_is_stale": True,
            "indicator_stale_threshold_seconds": threshold,
            "bars_available": 0,
            "warmup_requirements": {},
            "timestamp": _now_iso_ist(),
        }

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
                    "timestamp": _now_iso_ist(),
                }
            )
        # fallback to Redis discovery
        discovered = await asyncio.to_thread(_discover_instruments_from_redis, 50)
    except Exception:
        discovered = await asyncio.to_thread(_discover_instruments_from_redis, 50)
    instruments = discovered or ([DEFAULT_INSTRUMENT] if DEFAULT_INSTRUMENT else [])
    return _normalize_timestamp_fields(
        {
            "instruments": instruments,
            "count": len(instruments),
            "timestamp": _now_iso_ist(),
        }
    )

async def get_market_depth(instrument: str):
    """Get market depth (order book) for an instrument"""
    cache_key = instrument
    upstream_error: Optional[str] = None
    mode_hint = _get_current_mode_hint()
    try:
        if mode_hint != "historical":
            # First try the Market Data API outside replay mode.
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
                out["timestamp"] = _now_iso_ist()
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
                "timestamp": _normalize_timestamp_string(timestamp) or _now_iso_ist(),
                "status": "no_data",
                "warning": (None if mode_hint == "historical" else upstream_error),
            }, mode_hint=mode_hint, default_status="no_data")
        
        buy_levels = json.loads(buy_data)
        sell_levels = json.loads(sell_data)
        
        out = {
            "instrument": instrument,
            "buy": buy_levels[:5],  # Top 5 bids
            "sell": sell_levels[:5],  # Top 5 asks
            "timestamp": _normalize_timestamp_string(timestamp) or _now_iso_ist(),
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
            out["timestamp"] = _now_iso_ist()
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
            "timestamp": _now_iso_ist(),
            "error": str(e),
            "status": "error"
        }, mode_hint=mode_hint, default_status="error")

async def get_options_chain(instrument: str, expiry: str = None):
    """Get options chain for an instrument"""
    cache_key = f"{instrument}:{expiry or 'default'}"
    upstream_error: Optional[str] = None
    mode_hint = _get_current_mode_hint()
    try:
        # First try the Market Data API outside replay mode.
        params = {"expiry": expiry} if expiry else {}
        if mode_hint != "historical":
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
                out["timestamp"] = _now_iso_ist()
                return _normalize_options_contract(
                    instrument,
                    out,
                    expiry=expiry,
                    mode_hint=mode_hint,
                    default_status="stale",
                )

            if mode_hint == "historical":
                snapshot_payload = _historical_options_payload_from_snapshot(instrument)
                if snapshot_payload:
                    snapshot_payload = _normalize_options_contract(
                        instrument,
                        snapshot_payload,
                        expiry=expiry,
                        mode_hint=mode_hint,
                        default_status="ok",
                    )
                    _LAST_GOOD_OPTIONS[cache_key] = snapshot_payload
                    return snapshot_payload

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
                "timestamp": _now_iso_ist(),
                "status": "no_data",
                "mode_hint": mode_hint,
                "message": message,
                "warning": (None if mode_hint == "historical" else upstream_error),
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
            out["timestamp"] = _now_iso_ist()
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
            "timestamp": _now_iso_ist(),
            "error": str(e),
            "status": "error"
        }, expiry=expiry, mode_hint=mode_hint, default_status="error")


_market_data_routes = DashboardMarketDataRouter(
    get_redis_mongo_sync_lag_fn=get_redis_mongo_sync_lag,
    market_data_status_fn=market_data_status,
    validate_data_availability_fn=validate_data_availability,
    get_ohlc_data_fn=get_ohlc_data,
    get_chart_data_fn=get_chart_data,
    get_technical_indicators_fn=get_technical_indicators,
    get_available_instruments_fn=get_available_instruments,
    get_market_depth_fn=get_market_depth,
    get_options_chain_fn=get_options_chain,
)
app.include_router(_market_data_routes.router)
_public_contract_routes.bind_market_data_handlers(
    get_ohlc_data=_market_data_routes.get_ohlc_data,
    get_technical_indicators=_market_data_routes.get_technical_indicators,
    get_market_depth=_market_data_routes.get_market_depth,
    get_options_chain=_market_data_routes.get_options_chain,
)

get_redis_mongo_sync_lag = _market_data_routes.get_redis_mongo_sync_lag
market_data_status = _market_data_routes.market_data_status
validate_data_availability = _market_data_routes.validate_data_availability
get_ohlc_data = _market_data_routes.get_ohlc_data
get_chart_data = _market_data_routes.get_chart_data
get_technical_indicators = _market_data_routes.get_technical_indicators
get_available_instruments = _market_data_routes.get_available_instruments
get_market_depth = _market_data_routes.get_market_depth
get_options_chain = _market_data_routes.get_options_chain


# ============================================================================
# SIMPLE/FAST ENDPOINTS - Direct Redis Access (No Complex Processing)
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("MARKET_DATA_DASHBOARD_PORT", "8008"))
    uvicorn.run(app, host="0.0.0.0", port=port)
