"""Live depth poller — writes ATM CE/PE depth to Redis (latest) AND Mongo (history).

Runs inside the ingestion_app container. Calls the Kite REST API for each
instrument in DEPTH_FEED_INSTRUMENTS, captures full 5-level bid/ask depth,
derives direction-useful metrics, then:

- writes the *latest* tick to Redis (low-latency, TTL=60s) so strategy_app can
  read it without a Mongo round-trip
- appends the same tick to Mongo `market_depth_ticks` so the full intraday
  ladder is available for direction analysis / research

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

DEPTH_MONGO_ENABLED
    Set to "0" to disable Mongo persistence (Redis-only). Default "1".

DEPTH_MONGO_TTL_DAYS
    Auto-expire Mongo depth docs after this many days (default 7).

Redis keys written (prefixed by execution mode):
    depth:atm_ce:latest   — for any CE instrument
    depth:atm_pe:latest   — for any PE instrument

Mongo collection:
    trading_ai.market_depth_ticks
    indexed on (instrument, fetched_at_epoch) and (trade_date_ist, instrument)
    TTL index on fetched_at (default 7 days)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import redis

from contracts_app import get_redis_key, redis_connection_kwargs

try:
    from pymongo import ASCENDING, MongoClient
    from pymongo.collection import Collection
except Exception:  # pragma: no cover
    MongoClient = None  # type: ignore
    ASCENDING = 1
    Collection = None  # type: ignore

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_DEFAULT_POLL_SEC = 5
_DEFAULT_TTL_SEC = 60
_DEFAULT_OPEN = (9, 15)
_DEFAULT_CLOSE = (15, 35)
_MONGO_COLLECTION = "market_depth_ticks"
_DEFAULT_MONGO_TTL_DAYS = 7


def _env_str(name: str, default: str = "") -> str:
    return str(os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env_str(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


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


def _normalize_levels(levels: List[Dict[str, Any]], max_levels: int = 5) -> List[Dict[str, Any]]:
    """Coerce Kite depth levels to {price, qty, orders} with up to 5 entries."""
    out: List[Dict[str, Any]] = []
    for level in (levels or [])[:max_levels]:
        try:
            price = float(level.get("price") or 0.0)
            qty = int(level.get("quantity") or 0)
            orders = int(level.get("orders") or 0)
        except (TypeError, ValueError):
            continue
        out.append({"price": price, "qty": qty, "orders": orders})
    while len(out) < max_levels:
        out.append({"price": 0.0, "qty": 0, "orders": 0})
    return out


def _compute_derived(
    bid_levels: List[Dict[str, Any]],
    ask_levels: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute direction-useful metrics from 5-level depth."""
    best_bid = bid_levels[0]["price"] if bid_levels and bid_levels[0]["price"] > 0 else None
    best_ask = ask_levels[0]["price"] if ask_levels and ask_levels[0]["price"] > 0 else None
    bid_qty = bid_levels[0]["qty"] if bid_levels else 0
    ask_qty = ask_levels[0]["qty"] if ask_levels else 0

    spread = None
    mid = None
    microprice = None
    if best_bid is not None and best_ask is not None and best_ask >= best_bid > 0:
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2.0
        total_top = bid_qty + ask_qty
        if total_top > 0:
            # qty-weighted mid: more weight where there's less competing flow
            microprice = (best_bid * ask_qty + best_ask * bid_qty) / total_top

    total_bid_qty = sum(l["qty"] for l in bid_levels)
    total_ask_qty = sum(l["qty"] for l in ask_levels)
    qty_imbalance = None
    if total_bid_qty + total_ask_qty > 0:
        qty_imbalance = (total_bid_qty - total_ask_qty) / (total_bid_qty + total_ask_qty)

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "spread": spread,
        "mid": mid,
        "microprice": microprice,
        "total_bid_qty": total_bid_qty,
        "total_ask_qty": total_ask_qty,
        "qty_imbalance": qty_imbalance,
    }


def _side_key(instrument: str) -> Optional[str]:
    """Map instrument symbol to Redis key suffix (ce or pe)."""
    upper = instrument.strip().upper()
    if upper.endswith("CE"):
        return "depth:atm_ce:latest"
    if upper.endswith("PE"):
        return "depth:atm_pe:latest"
    return None


def _build_record(instrument: str, quote: Dict[str, Any]) -> Dict[str, Any]:
    """Build a single depth record (Redis + Mongo)."""
    now_ist = _now_ist()
    depth_raw = quote.get("depth") if isinstance(quote.get("depth"), dict) else {}
    bid_levels = _normalize_levels(depth_raw.get("buy") or [])
    ask_levels = _normalize_levels(depth_raw.get("sell") or [])
    derived = _compute_derived(bid_levels, ask_levels)

    return {
        "instrument": instrument,
        "trade_date_ist": now_ist.strftime("%Y-%m-%d"),
        "fetched_at": now_ist,
        "fetched_at_ist": now_ist.isoformat(),
        "fetched_at_epoch": now_ist.timestamp(),
        # back-compat fields used by RedisDepthReader
        "best_bid": derived["best_bid"],
        "best_ask": derived["best_ask"],
        "bid_qty": derived["bid_qty"],
        "ask_qty": derived["ask_qty"],
        # new: full ladder + derived metrics for direction analysis
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "spread": derived["spread"],
        "mid": derived["mid"],
        "microprice": derived["microprice"],
        "total_bid_qty": derived["total_bid_qty"],
        "total_ask_qty": derived["total_ask_qty"],
        "qty_imbalance": derived["qty_imbalance"],
        # optional from kite quote
        "last_price": quote.get("last_price"),
        "volume": quote.get("volume"),
        "oi": quote.get("oi"),
    }


def _redis_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """Flat schema for strategy_app RedisDepthReader (live side-channel)."""
    return {
        "instrument": record["instrument"],
        "best_bid": record["best_bid"],
        "best_ask": record["best_ask"],
        "bid_qty": record["bid_qty"],
        "ask_qty": record["ask_qty"],
        "fetched_at": record["fetched_at_ist"],
        "fetched_at_epoch": record["fetched_at_epoch"],
        "microprice": record.get("microprice"),
        "qty_imbalance": record.get("qty_imbalance"),
        "total_bid_qty": record.get("total_bid_qty"),
        "total_ask_qty": record.get("total_ask_qty"),
    }


def _connect_mongo() -> Optional[Any]:
    """Open a Mongo client and ensure indexes on the depth collection."""
    if MongoClient is None:
        logger.warning("depth collector: pymongo not installed — Mongo persistence disabled")
        return None
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    try:
        if uri:
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        else:
            client = MongoClient(
                host=str(os.getenv("MONGO_HOST") or "localhost"),
                port=int(os.getenv("MONGO_PORT") or "27017"),
                serverSelectionTimeoutMS=5000,
            )
        client.admin.command("ping")
        db = client[db_name]
        _ensure_depth_indexes(db[_MONGO_COLLECTION])
        return db[_MONGO_COLLECTION]
    except Exception as exc:
        logger.warning("depth collector: Mongo connect failed (%s) — Redis-only mode", exc)
        return None


def _ensure_depth_indexes(coll: Any) -> None:
    """Create indexes for query patterns + TTL auto-expiry."""
    try:
        coll.create_index(
            [("instrument", ASCENDING), ("fetched_at_epoch", ASCENDING)],
            name="instrument_fetched_at",
            background=True,
        )
        coll.create_index(
            [("trade_date_ist", ASCENDING), ("instrument", ASCENDING)],
            name="trade_date_instrument",
            background=True,
        )
        ttl_days = max(1, _env_int("DEPTH_MONGO_TTL_DAYS", _DEFAULT_MONGO_TTL_DAYS))
        coll.create_index(
            [("fetched_at", ASCENDING)],
            name="fetched_at_ttl",
            expireAfterSeconds=ttl_days * 86400,
            background=True,
        )
    except Exception:
        logger.warning("depth collector: index creation failed (continuing)", exc_info=True)


def _poll_once(
    instruments: list[str],
    redis_client: redis.Redis,
    kite_client: Any,
    ttl_sec: int,
    mongo_coll: Optional[Any],
) -> None:
    """Fetch depth for all instruments and write to Redis + Mongo."""
    if not instruments:
        return

    try:
        quotes = kite_client.quote(instruments)
    except Exception:
        logger.warning("depth poll: kite quote failed", exc_info=True)
        return

    mongo_batch: List[Dict[str, Any]] = []

    for instrument in instruments:
        redis_key_suffix = _side_key(instrument)
        if redis_key_suffix is None:
            logger.debug("depth poll: skipping non-CE/PE instrument %s", instrument)
            continue

        quote = quotes.get(instrument) or {}
        record = _build_record(instrument, quote)

        try:
            redis_client.setex(
                get_redis_key(redis_key_suffix),
                ttl_sec,
                json.dumps(_redis_payload(record), default=str),
            )
        except Exception:
            logger.warning("depth poll: redis write failed for %s", instrument, exc_info=True)

        if mongo_coll is not None:
            mongo_batch.append(record)

    if mongo_coll is not None and mongo_batch:
        try:
            mongo_coll.insert_many(mongo_batch, ordered=False)
        except Exception:
            logger.warning("depth poll: mongo insert failed", exc_info=True)


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
    mongo_enabled = _env_bool("DEPTH_MONGO_ENABLED", True)

    api_key = _env_str("KITE_API_KEY")
    access_token = _env_str("KITE_ACCESS_TOKEN")
    if not api_key:
        logger.error("depth collector: KITE_API_KEY not set — aborting")
        return

    kite = create_kite_client(api_key=api_key, access_token=access_token or None)
    redis_client = redis.Redis(**redis_connection_kwargs(decode_responses=True))
    mongo_coll = _connect_mongo() if mongo_enabled else None

    logger.info(
        "depth collector started instruments=%s poll_sec=%s open=%s:%02d close=%s:%02d mongo=%s",
        instruments,
        poll_sec,
        *open_hm,
        *close_hm,
        "on" if mongo_coll is not None else "off",
    )

    while True:
        now = _now_ist()
        if _is_market_hours(now, open_hm, close_hm):
            _poll_once(instruments, redis_client, kite, ttl_sec, mongo_coll)
        else:
            logger.debug("depth collector: outside market hours (%02d:%02d)", now.hour, now.minute)
        time.sleep(poll_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
