"""Asynchronous MongoDB persistence for live market events.

This module is intentionally decoupled from hot paths:
- Producers only enqueue to a bounded in-memory queue (non-blocking).
- A background worker batches writes to MongoDB.
- When Mongo is unavailable, events are dropped after queue saturation.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import threading
import time
from datetime import datetime
from queue import Empty, Full, Queue
from typing import Any, Dict, List, Optional

try:
    from pymongo import ASCENDING, MongoClient
except Exception:  # pragma: no cover - optional runtime dependency
    ASCENDING = 1
    MongoClient = None

from .time_utils import IST, minute_bucket_ist, parse_market_timestamp_ist, to_ist

logger = logging.getLogger(__name__)


def mongo_config() -> dict[str, Any]:
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        return {"uri": uri}
    return {
        "host": str(os.getenv("MONGO_HOST") or "localhost"),
        "port": int(os.getenv("MONGO_PORT") or "27017"),
        "db": str(os.getenv("MONGO_DB") or "trading_ai"),
    }


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _ist_now() -> datetime:
    return datetime.now(tz=IST)


_CONTRACT_RE = re.compile(r"^([A-Z]+)\d{2}[A-Z]{3}(?:\d+)?(?:FUT|CE|PE)$")


def extract_underlying_symbol(symbol: Optional[str]) -> str:
    text = str(symbol or "").strip().upper().replace(" ", "")
    if not text:
        return ""
    match = _CONTRACT_RE.match(text)
    if match:
        return str(match.group(1))
    if text.endswith("-I") or text.endswith("-II") or text.endswith("-III"):
        return text.split("-", 1)[0]
    return re.sub(r"[^A-Z]", "", text)


def is_futures_symbol(symbol: Optional[str]) -> bool:
    text = str(symbol or "").strip().upper().replace(" ", "")
    return text.endswith("FUT")


def _parse_ts(value: Any) -> Optional[datetime]:
    return parse_market_timestamp_ist(value)


def _minute_bucket(dt: datetime) -> datetime:
    return minute_bucket_ist(dt)


class MongoPersistenceSink:
    def __init__(self) -> None:
        self.enabled = _env_bool("MONGO_PERSIST_ENABLED", True)
        self.live_only = _env_bool("MONGO_PERSIST_LIVE_ONLY", True)
        self.include_non_real = _env_bool("MONGO_PERSIST_INCLUDE_NON_REAL", False)
        self.queue_size = max(1000, int(os.getenv("MONGO_PERSIST_QUEUE_SIZE", "50000")))
        self.batch_size = max(10, int(os.getenv("MONGO_PERSIST_BATCH_SIZE", "250")))
        self.flush_interval_seconds = max(
            0.05, float(os.getenv("MONGO_PERSIST_FLUSH_INTERVAL_SECONDS", "1.0"))
        )
        self.connect_retry_seconds = max(
            2.0, float(os.getenv("MONGO_PERSIST_CONNECT_RETRY_SECONDS", "15"))
        )

        self.tick_collection = os.getenv("MONGO_COLL_TICKS", "live_ticks")
        self.options_collection = os.getenv("MONGO_COLL_OPTIONS", "live_options_chain")
        self.depth_collection = os.getenv("MONGO_COLL_DEPTH", "live_depth")
        self.snapshot_collection = os.getenv("MONGO_COLL_SNAPSHOTS", "phase1_market_snapshots")

        self._queue: Queue[Dict[str, Any]] = Queue(maxsize=self.queue_size)
        self._client = None
        self._db = None
        self._indexes_ready = False
        self._last_connect_attempt = 0.0
        self._dropped = 0
        self._insert_failures = 0

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.enabled:
            return
        if MongoClient is None:
            logger.warning("Mongo persistence enabled but pymongo is unavailable")
            self.enabled = False
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="mongo-persist-worker", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=3.0)
        self._thread = None

    def should_persist(self, *, mode: Optional[str], source: Optional[str]) -> bool:
        if not self.enabled:
            return False
        mode_text = str(mode or "").strip().lower()
        if self.live_only and mode_text != "live":
            return False
        source_text = str(source or "").strip().lower()
        if (not self.include_non_real) and any(
            token in source_text for token in ("synthetic", "mock", "fallback")
        ):
            return False
        return True

    def persist_tick(
        self,
        *,
        tick_payload: Dict[str, Any],
        mode: Optional[str],
        source: str,
        instrument: Optional[str] = None,
    ) -> bool:
        if not self.should_persist(mode=mode, source=source):
            return False
        payload = dict(tick_payload or {})
        instrument_upper = str(instrument or payload.get("instrument") or "").upper()
        if not instrument_upper:
            return False
        ts = _parse_ts(payload.get("market_timestamp") or payload.get("timestamp")) or _ist_now()
        doc = {
            "event_type": "tick",
            "mode": str(mode or "").lower(),
            "source": str(source),
            "instrument": instrument_upper,
            "underlying_symbol": extract_underlying_symbol(instrument_upper),
            "timestamp": ts,
            "market_minute": _minute_bucket(ts),
            "received_at_ist": _ist_now(),
            "last_price": payload.get("last_price"),
            "candle_volume": payload.get("candle_volume"),
            "cumulative_volume": payload.get("cumulative_volume"),
            "oi": payload.get("oi"),
            "payload": payload,
        }
        return self._enqueue({"event_type": "tick", "doc": doc})

    def persist_options_chain(
        self,
        *,
        snapshot: Dict[str, Any],
        mode: Optional[str],
        source: str,
        instrument: Optional[str] = None,
    ) -> bool:
        if not self.should_persist(mode=mode, source=source):
            return False
        snap = dict(snapshot or {})
        instrument_upper = str(instrument or snap.get("instrument") or "").upper()
        if not instrument_upper:
            return False
        underlying = str(snap.get("underlying_symbol") or extract_underlying_symbol(instrument_upper)).upper()
        futures_instrument = snap.get("futures_instrument")
        if not futures_instrument and is_futures_symbol(instrument_upper):
            futures_instrument = instrument_upper
        ts = _parse_ts(snap.get("timestamp")) or _ist_now()
        ts_ist = to_ist(ts)
        strikes = snap.get("strikes") or []
        doc = {
            "event_type": "options_chain",
            "mode": str(mode or "").lower(),
            "source": str(source),
            "instrument": instrument_upper,
            "underlying_symbol": underlying,
            "futures_instrument": str(futures_instrument).upper() if futures_instrument else None,
            "timestamp": ts,
            "market_minute": _minute_bucket(ts),
            "trade_date_ist": ts_ist.date().isoformat(),
            "market_time_ist": ts_ist.strftime("%H:%M:%S"),
            "received_at_ist": _ist_now(),
            "expiry": snap.get("expiry"),
            "strike_count": len(strikes) if isinstance(strikes, list) else 0,
            "futures_price": snap.get("futures_price"),
            "pcr": snap.get("pcr"),
            "max_pain": snap.get("max_pain"),
            "snapshot": snap,
        }
        return self._enqueue({"event_type": "options_chain", "doc": doc})

    def persist_depth(
        self,
        *,
        instrument: str,
        buy_depth: Optional[List[Dict[str, Any]]],
        sell_depth: Optional[List[Dict[str, Any]]],
        timestamp: Optional[str],
        mode: Optional[str],
        source: str,
    ) -> bool:
        if not self.should_persist(mode=mode, source=source):
            return False
        instrument_upper = str(instrument or "").upper()
        if not instrument_upper:
            return False
        buy = list(buy_depth or [])
        sell = list(sell_depth or [])
        ts = _parse_ts(timestamp) or _ist_now()
        total_bid_qty = float(sum(float(level.get("quantity", 0) or 0) for level in buy))
        total_ask_qty = float(sum(float(level.get("quantity", 0) or 0) for level in sell))
        doc = {
            "event_type": "depth",
            "mode": str(mode or "").lower(),
            "source": str(source),
            "instrument": instrument_upper,
            "underlying_symbol": extract_underlying_symbol(instrument_upper),
            "timestamp": ts,
            "market_minute": _minute_bucket(ts),
            "received_at_ist": _ist_now(),
            "buy_depth": buy,
            "sell_depth": sell,
            "total_bid_qty": total_bid_qty,
            "total_ask_qty": total_ask_qty,
        }
        return self._enqueue({"event_type": "depth", "doc": doc})

    def persist_snapshot_event(
        self,
        *,
        event_payload: Dict[str, Any],
        mode: Optional[str],
        source: str,
    ) -> bool:
        if not self.should_persist(mode=mode, source=source):
            return False
        payload = dict(event_payload or {})
        snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
        session_context = snapshot.get("session_context") if isinstance(snapshot.get("session_context"), dict) else {}
        instrument = str(snapshot.get("instrument") or "").strip().upper()

        ts = (
            _parse_ts(session_context.get("timestamp"))
            or _parse_ts(payload.get("published_at"))
            or _ist_now()
        )
        ts_ist = to_ist(ts)

        doc = {
            "event_type": "snapshot",
            "mode": str(mode or "").lower(),
            "source": str(source),
            "instrument": instrument,
            "snapshot_id": str(payload.get("snapshot_id") or snapshot.get("snapshot_id") or ""),
            "event_id": str(payload.get("event_id") or ""),
            "timestamp": ts,
            "market_minute": _minute_bucket(ts),
            "trade_date_ist": ts_ist.date().isoformat(),
            "market_time_ist": ts_ist.strftime("%H:%M:%S"),
            "received_at_ist": _ist_now(),
            "payload": payload,
        }
        return self._enqueue({"event_type": "snapshot", "doc": doc})

    def _enqueue(self, event: Dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except Full:
            self._dropped += 1
            if self._dropped <= 5 or self._dropped % 1000 == 0:
                logger.warning("Mongo persistence queue full, dropped=%s", self._dropped)
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._ensure_connected():
                time.sleep(min(self.connect_retry_seconds, 1.0))
                continue
            batch = self._dequeue_batch()
            if not batch:
                continue
            self._flush(batch)

        # Best-effort drain on shutdown.
        while self._ensure_connected():
            batch = self._dequeue_batch(non_blocking=True)
            if not batch:
                break
            self._flush(batch)

    def _dequeue_batch(self, non_blocking: bool = False) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            timeout = 0.0 if non_blocking else self.flush_interval_seconds
            first = self._queue.get(timeout=timeout)
            out.append(first)
        except Empty:
            return out

        while len(out) < self.batch_size:
            try:
                out.append(self._queue.get_nowait())
            except Empty:
                break
        return out

    def _ensure_connected(self) -> bool:
        if self._db is not None:
            return True

        now = time.monotonic()
        if (now - self._last_connect_attempt) < self.connect_retry_seconds:
            return False
        self._last_connect_attempt = now

        if MongoClient is None:
            self.enabled = False
            return False

        cfg = mongo_config()
        db_name = str(os.getenv("MONGO_DB") or cfg.get("db") or "trading_ai")
        try:
            if "uri" in cfg:
                client = MongoClient(
                    cfg["uri"],
                    appname="market_data_persistence",
                    serverSelectionTimeoutMS=2000,
                    connectTimeoutMS=2000,
                    socketTimeoutMS=5000,
                )
            else:
                client = MongoClient(
                    host=str(cfg.get("host", "localhost")),
                    port=int(cfg.get("port", 27017)),
                    appname="market_data_persistence",
                    serverSelectionTimeoutMS=2000,
                    connectTimeoutMS=2000,
                    socketTimeoutMS=5000,
                )
            client.admin.command("ping")
            self._client = client
            self._db = client[db_name]
            self._ensure_indexes()
            return True
        except Exception as exc:
            logger.warning("Mongo persistence connect failed: %s", exc)
            self._client = None
            self._db = None
            return False

    def _ensure_indexes(self) -> None:
        if self._db is None or self._indexes_ready:
            return
        try:
            self._db[self.tick_collection].create_index(
                [("instrument", ASCENDING), ("market_minute", ASCENDING), ("timestamp", ASCENDING)]
            )
            self._db[self.options_collection].create_index(
                [("instrument", ASCENDING), ("market_minute", ASCENDING), ("expiry", ASCENDING)]
            )
            self._db[self.options_collection].create_index(
                [("instrument", ASCENDING), ("trade_date_ist", ASCENDING), ("timestamp", ASCENDING)]
            )
            self._db[self.options_collection].create_index(
                [("futures_instrument", ASCENDING), ("trade_date_ist", ASCENDING), ("timestamp", ASCENDING)]
            )
            self._db[self.depth_collection].create_index(
                [("instrument", ASCENDING), ("market_minute", ASCENDING), ("timestamp", ASCENDING)]
            )
            self._db[self.snapshot_collection].create_index(
                [("snapshot_id", ASCENDING), ("timestamp", ASCENDING)]
            )
            self._db[self.snapshot_collection].create_index(
                [("instrument", ASCENDING), ("trade_date_ist", ASCENDING), ("timestamp", ASCENDING)]
            )
            ttl_days = int(os.getenv("MONGO_PERSIST_TTL_DAYS", "0"))
            if ttl_days > 0:
                ttl_seconds = int(ttl_days * 24 * 60 * 60)
                self._db[self.tick_collection].create_index("received_at_ist", expireAfterSeconds=ttl_seconds)
                self._db[self.options_collection].create_index("received_at_ist", expireAfterSeconds=ttl_seconds)
                self._db[self.depth_collection].create_index("received_at_ist", expireAfterSeconds=ttl_seconds)
                self._db[self.snapshot_collection].create_index("received_at_ist", expireAfterSeconds=ttl_seconds)
            self._indexes_ready = True
        except Exception as exc:
            logger.warning("Mongo persistence index setup failed: %s", exc)

    def _flush(self, batch: List[Dict[str, Any]]) -> None:
        if self._db is None:
            return

        grouped: Dict[str, List[Dict[str, Any]]] = {
            "tick": [],
            "options_chain": [],
            "depth": [],
            "snapshot": [],
        }
        for item in batch:
            event_type = str(item.get("event_type") or "")
            doc = item.get("doc")
            if event_type in grouped and isinstance(doc, dict):
                grouped[event_type].append(doc)

        mapping = {
            "tick": self.tick_collection,
            "options_chain": self.options_collection,
            "depth": self.depth_collection,
            "snapshot": self.snapshot_collection,
        }
        for event_type, docs in grouped.items():
            if not docs:
                continue
            collection_name = mapping[event_type]
            try:
                self._db[collection_name].insert_many(docs, ordered=False)
            except Exception as exc:
                self._insert_failures += len(docs)
                logger.warning(
                    "Mongo persistence insert failed for %s (%s docs, failures=%s): %s",
                    collection_name,
                    len(docs),
                    self._insert_failures,
                    exc,
                )


_SINK_LOCK = threading.Lock()
_SINK: Optional[MongoPersistenceSink] = None


def get_mongo_persistence_sink() -> MongoPersistenceSink:
    global _SINK
    with _SINK_LOCK:
        if _SINK is None:
            _SINK = MongoPersistenceSink()
            _SINK.start()
        return _SINK


def persist_tick_async(
    *,
    tick_payload: Dict[str, Any],
    mode: Optional[str],
    source: str,
    instrument: Optional[str] = None,
) -> bool:
    sink = get_mongo_persistence_sink()
    return sink.persist_tick(
        tick_payload=tick_payload,
        mode=mode,
        source=source,
        instrument=instrument,
    )


def persist_options_chain_async(
    *,
    snapshot: Dict[str, Any],
    mode: Optional[str],
    source: str,
    instrument: Optional[str] = None,
) -> bool:
    sink = get_mongo_persistence_sink()
    return sink.persist_options_chain(
        snapshot=snapshot,
        mode=mode,
        source=source,
        instrument=instrument,
    )


def persist_depth_async(
    *,
    instrument: str,
    buy_depth: Optional[List[Dict[str, Any]]],
    sell_depth: Optional[List[Dict[str, Any]]],
    timestamp: Optional[str],
    mode: Optional[str],
    source: str,
) -> bool:
    sink = get_mongo_persistence_sink()
    return sink.persist_depth(
        instrument=instrument,
        buy_depth=buy_depth,
        sell_depth=sell_depth,
        timestamp=timestamp,
        mode=mode,
        source=source,
    )


def persist_snapshot_event_async(
    *,
    event_payload: Dict[str, Any],
    mode: Optional[str],
    source: str,
) -> bool:
    sink = get_mongo_persistence_sink()
    return sink.persist_snapshot_event(
        event_payload=event_payload,
        mode=mode,
        source=source,
    )
