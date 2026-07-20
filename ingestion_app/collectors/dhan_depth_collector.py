"""Live depth poller — Dhan port of depth_collector.py (2026-07-21).

depth_collector.py (Kite) has sat dead since the Dhan migration: it calls
kite_client.quote(), which nothing in this stack authenticates anymore.
DEPTH_FEED_ENABLED has been unset since, and NIFTY's composite direction
scorer's heaviest-weighted signal (ENTRY_DIR_W_DEPTH=1.1) has fired zero
times in production, undetected for weeks (see
ops/verify_direction_liveness.py, config_contract_expected.json's
weight_requires rule).

Verified live against the real account before writing this (2026-07-20/21):
Dhan's raw /v2/marketfeed/quote response already has depth.buy[]/depth.sell[]
with {price, quantity, orders} — the EXACT shape depth_collector.py's
_normalize_levels/_compute_derived/_build_record/_redis_payload already
parse. Only the API call, credentials, and instrument resolution are
broker-specific; everything else is reused unchanged from depth_collector.py.

Auto ATM rotation (an improvement over the old Kite runbook's manual
"identify + update DEPTH_FEED_INSTRUMENTS by hand" process): resolves
today's ATM strike from the index quote every poll, re-resolving CE/PE
security_ids only when the ATM strike actually changes.

ENV VARS (Dhan-specific; poll/TTL/market-hours/mongo vars unchanged from
depth_collector.py, see that module's docstring)
------------------------------------------------
DEPTH_UNDERLYING        BANKNIFTY | NIFTY (default BANKNIFTY)
DEPTH_STRIKE_STEP       Strike spacing for ATM rounding (default: 100 for
                        BANKNIFTY, 50 for NIFTY)
DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID   Same credentials as execution_app/
                        ingestion_app — this collector is recreated by the
                        same daily dhan-token-refresh cycle, so no separate
                        file-based reload logic is needed (unlike the old
                        Kite version, which had to self-heal mid-session).

Redis keys and Mongo collection: identical to depth_collector.py
(depth:atm_ce:latest / depth:atm_pe:latest / market_depth_ticks) — the
strategy_app consumer (RedisDepthReader / depth_context.py) needs no
changes regardless of which broker wrote the key.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import time
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import redis

from contracts_app import redis_connection_kwargs

from .depth_collector import (  # broker-agnostic, reused verbatim
    _DEFAULT_CLOSE,
    _DEFAULT_OPEN,
    _DEFAULT_POLL_SEC,
    _DEFAULT_TTL_SEC,
    _build_record,
    _connect_mongo,
    _env_bool,
    _env_int,
    _env_str,
    _is_market_hours,
    _now_ist,
    _parse_hhmm,
    _redis_payload,
)

logger = logging.getLogger(__name__)

_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
_DHAN_BASE = "https://api.dhan.co/v2"
_INDEX_SECURITY_ID = {"BANKNIFTY": "25", "NIFTY": "13"}
_DEFAULT_STRIKE_STEP = {"BANKNIFTY": 100, "NIFTY": 50}


# ── Scrip master: (expiry, strike, CE/PE) -> security_id ───────────────────
# Self-contained (not imported from execution_app.adapter.dhan): ingestion_app's
# Docker image doesn't include execution_app's source, confirmed 2026-07-21.
# Same CSV, same parsing as that adapter's _ScripMaster -- proven live there.
class _ScripIndex:
    def __init__(self, underlying: str, cache_path: Optional[str] = None):
        self._underlying = underlying.strip().upper()
        self._cache_path = cache_path or os.path.join(
            os.getenv("TMPDIR", "/tmp"), f"dhan_scrip_master_{self._underlying.lower()}.csv"
        )
        self._index: Dict[Tuple[str, int, str], str] = {}
        self._loaded_for: Optional[date] = None

    def _raw_csv(self) -> str:
        today = datetime.utcnow().date()
        try:
            if os.path.exists(self._cache_path):
                mtime = datetime.utcfromtimestamp(os.path.getmtime(self._cache_path)).date()
                if mtime == today:
                    with open(self._cache_path, "r", encoding="utf-8", errors="replace") as fh:
                        return fh.read()
        except OSError:
            pass
        logger.info("depth collector (dhan): downloading scrip master")
        data = urllib.request.urlopen(_SCRIP_MASTER_URL, timeout=60).read().decode(
            "utf-8", errors="replace"
        )
        try:
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                fh.write(data)
        except OSError as exc:
            logger.warning("depth collector (dhan): scrip cache write failed (%s)", exc)
        return data

    def _ensure_loaded(self) -> None:
        today = datetime.utcnow().date()
        if self._loaded_for == today and self._index:
            return
        index: Dict[Tuple[str, int, str], str] = {}
        reader = csv.DictReader(io.StringIO(self._raw_csv()))
        for row in reader:
            if (row.get("SEM_INSTRUMENT_NAME") or "") != "OPTIDX":
                continue
            if self._underlying not in (row.get("SEM_TRADING_SYMBOL") or "").upper():
                continue
            opt = (row.get("SEM_OPTION_TYPE") or "").upper()
            if opt not in ("CE", "PE"):
                continue
            expiry = str(row.get("SEM_EXPIRY_DATE") or "").split(" ")[0].strip()
            try:
                strike = int(round(float(row.get("SEM_STRIKE_PRICE"))))
            except (TypeError, ValueError):
                continue
            sec_id = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
            if not expiry or not sec_id:
                continue
            index[(expiry, strike, opt)] = sec_id
        self._index = index
        self._loaded_for = today
        logger.info(
            "depth collector (dhan): indexed %d %s option contracts",
            len(index), self._underlying,
        )

    def nearest_expiry(self) -> Optional[str]:
        self._ensure_loaded()
        today = date.today().isoformat()
        expiries = sorted({k[0] for k in self._index})
        future = [e for e in expiries if e >= today]
        return future[0] if future else None

    def resolve(self, expiry: str, strike: int, option_type: str) -> Optional[str]:
        self._ensure_loaded()
        return self._index.get((expiry, int(strike), option_type.upper()))


# ── Dhan quote client (minimal — shares DhanLiveClient's proven /marketfeed/quote
# call pattern, kept local so this module has no import dependency on the wider
# ingestion_app package beyond the sibling depth_collector helpers above) ───────
class _DhanQuoteClient:
    def __init__(self, token: str, client_id: str):
        import requests
        self._session = requests.Session()
        self._session.headers.update({
            "access-token": token, "client-id": client_id, "Content-Type": "application/json",
        })
        self._last_call = 0.0

    def _throttle(self) -> None:
        # Quote API limit: 1 request/sec (dhanhq skill, verified).
        gap = time.monotonic() - self._last_call
        if gap < 1.0:
            time.sleep(1.0 - gap)
        self._last_call = time.monotonic()

    def get_quotes(self, securities: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
        """Returns {securityId: quote_dict}."""
        payload: Dict[str, List[int]] = {}
        for sec in securities:
            seg = str(sec.get("exchangeSegment") or "")
            sid = str(sec.get("securityId") or "")
            if seg and sid:
                payload.setdefault(seg, []).append(int(sid))
        if not payload:
            return {}
        self._throttle()
        r = self._session.post(f"{_DHAN_BASE}/marketfeed/quote", json=payload, timeout=30)
        if r.status_code == 429:
            logger.warning("depth poll (dhan): rate-limited")
            return {}
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        out: Dict[str, Dict[str, Any]] = {}
        for seg, by_sid in (data.items() if isinstance(data, dict) else []):
            if isinstance(by_sid, dict):
                for sid, quote in by_sid.items():
                    out[sid] = quote
        return out


def _classify_dhan_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "401" in text or "unauthor" in text or "token" in text or "invalid" in text:
        return "credential"
    if "timeout" in text or "connection" in text:
        return "network"
    return "unknown"


def _resolve_atm_and_legs(
    quote_client: _DhanQuoteClient,
    scrips: _ScripIndex,
    underlying: str,
    strike_step: int,
) -> Optional[tuple[int, str, str]]:
    """Returns (atm_strike, ce_security_id, pe_security_id) or None."""
    idx_sid = _INDEX_SECURITY_ID.get(underlying)
    if not idx_sid:
        return None
    quotes = quote_client.get_quotes([{"exchangeSegment": "IDX_I", "securityId": idx_sid}])
    quote = quotes.get(idx_sid) or {}
    spot = quote.get("last_price")
    if not spot or not math.isfinite(float(spot)) or float(spot) <= 0:
        return None
    atm = int(round(float(spot) / strike_step) * strike_step)
    expiry = scrips.nearest_expiry()
    if not expiry:
        return None
    ce_sid = scrips.resolve(expiry, atm, "CE")
    pe_sid = scrips.resolve(expiry, atm, "PE")
    if not ce_sid or not pe_sid:
        return None
    return atm, ce_sid, pe_sid


def _poll_once(
    quote_client: _DhanQuoteClient,
    scrips: _ScripIndex,
    underlying: str,
    strike_step: int,
    redis_client: redis.Redis,
    ttl_sec: int,
    mongo_coll: Optional[Any],
    last_atm: Optional[int],
) -> tuple[Optional[str], Optional[int]]:
    """Returns (error_or_None, atm_used_or_None)."""
    try:
        resolved = _resolve_atm_and_legs(quote_client, scrips, underlying, strike_step)
    except Exception as exc:
        logger.warning("depth poll (dhan): ATM/leg resolution failed", exc_info=True)
        return _classify_dhan_error(exc), last_atm
    if resolved is None:
        return None, last_atm
    atm, ce_sid, pe_sid = resolved
    if atm != last_atm:
        logger.info("depth collector (dhan): ATM strike now %s (was %s)", atm, last_atm)

    legs = [
        (f"{underlying}_ATM_CE", "NSE_FNO", ce_sid),
        (f"{underlying}_ATM_PE", "NSE_FNO", pe_sid),
    ]
    try:
        quotes = quote_client.get_quotes(
            [{"exchangeSegment": seg, "securityId": sid} for _, seg, sid in legs]
        )
    except Exception as exc:
        logger.warning("depth poll (dhan): leg quote failed", exc_info=True)
        return _classify_dhan_error(exc), atm

    # Instrument-namespaced keys (2026-07-21): matches
    # strategy_app/runtime/redis_depth_reader.py's STRATEGY_INSTRUMENT-based
    # lookup -- BankNifty and NIFTY collectors must not collide on one shared
    # "the ATM CE" key when both run at once.
    underlying_lower = underlying.lower()
    side_keys = {
        "CE": f"depth:{underlying_lower}:atm_ce:latest",
        "PE": f"depth:{underlying_lower}:atm_pe:latest",
    }
    mongo_batch: List[Dict[str, Any]] = []
    for label, _, sid in legs:
        side = "CE" if label.endswith("CE") else "PE"
        redis_key_suffix = side_keys[side]
        quote = quotes.get(sid) or {}
        record = _build_record(label, quote)
        try:
            import json
            from contracts_app import get_redis_key
            redis_client.setex(
                get_redis_key(redis_key_suffix), ttl_sec,
                json.dumps(_redis_payload(record), default=str),
            )
        except Exception:
            logger.warning("depth poll (dhan): redis write failed for %s", label, exc_info=True)
        if mongo_coll is not None:
            mongo_batch.append(record)

    if mongo_coll is not None and mongo_batch:
        try:
            mongo_coll.insert_many(mongo_batch, ordered=False)
        except Exception:
            logger.warning("depth poll (dhan): mongo insert failed", exc_info=True)

    return None, atm


def main() -> None:
    underlying = _env_str("DEPTH_UNDERLYING", "BANKNIFTY").upper()
    if underlying not in _INDEX_SECURITY_ID:
        logger.error("depth collector (dhan): unknown DEPTH_UNDERLYING=%s — aborting", underlying)
        return
    strike_step = _env_int("DEPTH_STRIKE_STEP", _DEFAULT_STRIKE_STEP.get(underlying, 100))
    poll_sec = max(2, _env_int("DEPTH_POLL_INTERVAL_SEC", _DEFAULT_POLL_SEC))
    ttl_sec = max(poll_sec * 2, _env_int("DEPTH_STALE_TTL_SEC", _DEFAULT_TTL_SEC))
    open_hm = _parse_hhmm(_env_str("DEPTH_MARKET_OPEN_IST", "09:15"), _DEFAULT_OPEN)
    close_hm = _parse_hhmm(_env_str("DEPTH_MARKET_CLOSE_IST", "15:35"), _DEFAULT_CLOSE)
    mongo_enabled = _env_bool("DEPTH_MONGO_ENABLED", True)

    token = _env_str("DHAN_ACCESS_TOKEN")
    client_id = _env_str("DHAN_CLIENT_ID")
    if not token or not client_id:
        logger.error("depth collector (dhan): DHAN_ACCESS_TOKEN/DHAN_CLIENT_ID not set — aborting")
        return

    quote_client = _DhanQuoteClient(token, client_id)
    scrips = _ScripIndex(underlying)
    redis_client = redis.Redis(**redis_connection_kwargs(decode_responses=True))
    mongo_coll = _connect_mongo() if mongo_enabled else None

    logger.info(
        "depth collector (dhan) started underlying=%s strike_step=%s poll_sec=%s "
        "open=%s:%02d close=%s:%02d mongo=%s",
        underlying, strike_step, poll_sec, *open_hm, *close_hm,
        "on" if mongo_coll is not None else "off",
    )

    last_atm: Optional[int] = None
    while True:
        now = _now_ist()
        if _is_market_hours(now, open_hm, close_hm):
            err, last_atm = _poll_once(
                quote_client, scrips, underlying, strike_step,
                redis_client, ttl_sec, mongo_coll, last_atm,
            )
            if err == "credential":
                logger.warning(
                    "depth collector (dhan): credential error — token likely rotated; "
                    "container will pick up the new token on next daily refresh recreate"
                )
        else:
            logger.debug("depth collector (dhan): outside market hours (%02d:%02d)", now.hour, now.minute)
        time.sleep(poll_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
