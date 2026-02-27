from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from contracts_app import parse_snapshot_event
from .time_utils import IST, parse_market_timestamp_ist, to_ist

try:
    from pymongo import ASCENDING, MongoClient
except Exception:  # pragma: no cover
    ASCENDING = 1
    MongoClient = None

def _parse_ts(value: Any) -> Optional[datetime]:
    return parse_market_timestamp_ist(value)


class SnapshotMongoWriter:
    def __init__(self) -> None:
        self.collection_name = str(os.getenv("MONGO_COLL_SNAPSHOTS") or "phase1_market_snapshots")
        self._client: Optional[Any] = None
        self._db: Optional[Any] = None
        self._indexes_ready = False

    def _db_handle(self) -> Optional[Any]:
        if self._db is not None:
            return self._db
        if MongoClient is None:
            return None

        uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
        db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
        if uri:
            client = MongoClient(uri, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000, socketTimeoutMS=5000)
        else:
            client = MongoClient(
                host=str(os.getenv("MONGO_HOST") or "localhost"),
                port=int(os.getenv("MONGO_PORT") or "27017"),
                serverSelectionTimeoutMS=2000,
                connectTimeoutMS=2000,
                socketTimeoutMS=5000,
            )
        client.admin.command("ping")
        self._client = client
        self._db = client[db_name]
        self._ensure_indexes()
        return self._db

    def _ensure_indexes(self) -> None:
        if self._db is None or self._indexes_ready:
            return
        coll = self._db[self.collection_name]
        coll.create_index([("snapshot_id", ASCENDING), ("timestamp", ASCENDING)])
        coll.create_index([("instrument", ASCENDING), ("trade_date_ist", ASCENDING), ("timestamp", ASCENDING)])
        ttl_days = int(os.getenv("MONGO_PERSIST_TTL_DAYS") or "0")
        if ttl_days > 0:
            coll.create_index("received_at_ist", expireAfterSeconds=int(ttl_days * 24 * 60 * 60))
        self._indexes_ready = True

    def write_snapshot_event(self, payload: dict[str, Any]) -> bool:
        event = parse_snapshot_event(payload)
        if event is None:
            return False
        db = self._db_handle()
        if db is None:
            return False

        snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
        session_context = snapshot.get("session_context") if isinstance(snapshot.get("session_context"), dict) else {}
        ts = _parse_ts(session_context.get("timestamp")) or _parse_ts(event.get("published_at")) or datetime.now(tz=IST)
        ts_ist = to_ist(ts)

        doc = {
            "event_type": "snapshot",
            "event_version": str(event.get("event_version") or "1.0"),
            "source": str(event.get("source") or "snapshot_app"),
            "event_id": str(event.get("event_id") or ""),
            "snapshot_id": str(event.get("snapshot_id") or ""),
            "instrument": str(snapshot.get("instrument") or "").strip().upper(),
            "timestamp": ts,
            "trade_date_ist": ts_ist.date().isoformat(),
            "market_time_ist": ts_ist.strftime("%H:%M:%S"),
            "received_at_ist": datetime.now(tz=IST),
            "payload": event,
        }
        db[self.collection_name].insert_one(doc)
        return True
