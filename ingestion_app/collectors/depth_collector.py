"""Live depth poller — writes ATM CE/PE best bid/ask to Redis every N seconds.

Runs inside the ingestion_app container. Calls the Kite REST API (via the
ingestion service) for each instrument in DEPTH_FEED_INSTRUMENTS and stores
a compact best-bid/ask record to Redis.

ENV VARS
--------
DEPTH_FEED_INSTRUMENTS
    Comma-separated Kite instrument symbols to poll.
    E.g. ``NFO:BANKNIFTY24AUG50000CE,NFO:BANKNIFTY24AUG50000PE``
    Required — collector sleeps if unset/empty.

DEPTH_POLL_INTERVAL_SEC
    Seconds between polls (default 5). Minimum 2.

DEPTH_STALE_TTL_SEC
    Redis key TTL in seconds (default 60). Keys auto-expire if collector stops.

DEPTH_MARKET_OPEN_IST   / DEPTH_MARKET_CLOSE_IST
    HH:MM boundaries outside which polling is skipped (default 09:15 / 15:35).

Redis keys written (prefixed by execution mode):
    depth:atm_ce:latest   — for any CE instrument
    depth:atm_pe:latest   — for any PE instrument

The strategy_app ``RedisDepthReader`` reads these keys.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import redis

from contracts_app import get_redis_key, redis_connection_kwargs

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_DEFAULT_POLL_SEC = 5
_DEFAULT_TTL_SEC = 60
_DEFAULT_OPEN = (9, 15)
_DEFAULT_CLOSE = (15, 35)


def _env_str(name: str, default: str = "") -> str:
    return str(os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _parse_hhmm(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = raw.strip().split(":")
        return int(h), int(m)
    except Exception:
        return default


def _is_market_hours(
    now: datetime,
    open_hm: tuple[int, int],
    close_hm: tuple[int, int],
) -> bool:
    cur = (now.hour, now.minute)
    return open_hm <= cur <= close_hm


def _extract_best(levels: list[dict[str, Any]]) -> tuple[Optional[float], Optional[int]]:
    """Return (price, qty) from the first non-zero level, else (None, None)."""
    for level in levels:
        price = level.get("price")
        qty = level.get("quantity")
        try:
            p = float(price)
            q = int(qty)
            if p > 0:
                return p, q
        except (TypeError, ValueError):
            continue
    return None, None


def _side_key(instrument: str) -> Optional[str]:
    """Map instrument symbol to Redis key suffix (ce or pe)."""
    upper = instrument.strip().upper()
    if upper.endswith("CE"):
        return "depth:atm_ce:latest"
    if upper.endswith("PE"):
        return "depth:atm_pe:latest"
    return None


def _poll_once(
    instruments: list[str],
    redis_client: redis.Redis,
    kite_client: Any,
    ttl_sec: int,
) -> None:
    """Fetch depth for all instruments and write to Redis."""
    if not instruments:
        return

    # Batch quote call — Kite allows multiple symbols in one request
    try:
        quotes = kite_client.quote(instruments)
    except Exception:
        logger.warning("depth poll: kite quote failed", exc_info=True)
        return

    now_epoch = time.time()
    fetched_at = datetime.now(tz=IST).isoformat()

    for instrument in instruments:
        redis_key_suffix = _side_key(instrument)
        if redis_key_suffix is None:
            logger.debug("depth poll: skipping non-CE/PE instrument %s", instrument)
            continue

        quote = quotes.get(instrument) or {}
        depth_raw = quote.get("depth") if isinstance(quote.get("depth"), dict) else {}
        buy_levels = list(depth_raw.get("buy") or [])
        sell_levels = list(depth_raw.get("sell") or [])

        best_bid, bid_qty = _extract_best(buy_levels)
        best_ask, ask_qty = _extract_best(sell_levels)

        record: Dict[str, Any] = {
            "instrument": instrument,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "fetched_at": fetched_at,
            "fetched_at_epoch": now_epoch,
        }

        try:
            redis_client.setex(
                get_redis_key(redis_key_suffix),
                ttl_sec,
                json.dumps(record, default=str),
            )
            logger.debug(
                "depth poll: %s bid=%.1f@%s ask=%.1f@%s",
                instrument,
                best_bid or 0,
                bid_qty or 0,
                best_ask or 0,
                ask_qty or 0,
            )
        except Exception:
            logger.warning("depth poll: redis write failed for %s", instrument, exc_info=True)


def main() -> None:
    from ingestion_app.kite_client import create_kite_client

    instruments_raw = _env_str("DEPTH_FEED_INSTRUMENTS")
    if not instruments_raw:
        logger.info("depth collector: DEPTH_FEED_INSTRUMENTS not set — sleeping")
        while True:
            time.sleep(60)
        return

    instruments = [s.strip() for s in instruments_raw.split(",") if s.strip()]
    poll_sec = max(2, _env_int("DEPTH_POLL_INTERVAL_SEC", _DEFAULT_POLL_SEC))
    ttl_sec = max(poll_sec * 2, _env_int("DEPTH_STALE_TTL_SEC", _DEFAULT_TTL_SEC))
    open_hm = _parse_hhmm(_env_str("DEPTH_MARKET_OPEN_IST", "09:15"), _DEFAULT_OPEN)
    close_hm = _parse_hhmm(_env_str("DEPTH_MARKET_CLOSE_IST", "15:35"), _DEFAULT_CLOSE)

    api_key = _env_str("KITE_API_KEY")
    access_token = _env_str("KITE_ACCESS_TOKEN")
    if not api_key:
        logger.error("depth collector: KITE_API_KEY not set — aborting")
        return

    kite = create_kite_client(api_key=api_key, access_token=access_token or None)
    redis_client = redis.Redis(**redis_connection_kwargs(decode_responses=True))

    logger.info(
        "depth collector started instruments=%s poll_sec=%s open=%s:%02d close=%s:%02d",
        instruments,
        poll_sec,
        *open_hm,
        *close_hm,
    )

    while True:
        now = _now_ist()
        if _is_market_hours(now, open_hm, close_hm):
            _poll_once(instruments, redis_client, kite, ttl_sec)
        else:
            logger.debug("depth collector: outside market hours (%02d:%02d)", now.hour, now.minute)
        time.sleep(poll_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
