from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pymongo import MongoClient

try:
    from contracts_app import TimestampSourceMode, isoformat_ist, parse_timestamp_to_ist
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from contracts_app import TimestampSourceMode, isoformat_ist, parse_timestamp_to_ist


def _mongo() -> MongoClient:
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        return MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000)
    return MongoClient(
        host=str(os.getenv("MONGO_HOST") or "localhost"),
        port=int(os.getenv("MONGO_PORT") or "27017"),
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
    )


def _collections() -> dict[str, str]:
    return {
        "phase1_market_snapshots": str(os.getenv("MONGO_COLL_SNAPSHOTS") or "phase1_market_snapshots"),
        "strategy_votes": str(os.getenv("MONGO_COLL_STRATEGY_VOTES") or "strategy_votes"),
        "trade_signals": str(os.getenv("MONGO_COLL_TRADE_SIGNALS") or "trade_signals"),
        "strategy_positions": str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS") or "strategy_positions"),
    }


def _normalized_top_level_timestamp(value: Any) -> str | None:
    parsed = parse_timestamp_to_ist(value, naive_mode=TimestampSourceMode.LEGACY_MONGO_UTC)
    return isoformat_ist(parsed) if parsed is not None else None


def _normalized_market_timestamp(value: Any) -> str | None:
    parsed = parse_timestamp_to_ist(value, naive_mode=TimestampSourceMode.MARKET_IST)
    return isoformat_ist(parsed) if parsed is not None else None


def _iter_updates(doc: dict[str, Any], coll_name: str) -> Iterator[tuple[str, Any]]:
    top_level = _normalized_top_level_timestamp(doc.get("timestamp"))
    if top_level is not None:
        yield "timestamp", top_level
        dt = parse_timestamp_to_ist(top_level)
        if dt is not None:
            yield "trade_date_ist", dt.date().isoformat()
            yield "market_time_ist", dt.strftime("%H:%M:%S")

    received = _normalized_market_timestamp(doc.get("received_at_ist"))
    if received is not None:
        yield "received_at_ist", received
        received_dt = parse_timestamp_to_ist(received)
        if received_dt is not None:
            yield "received_at_ttl", received_dt

    payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else None
    if not payload:
        return

    if coll_name == "phase1_market_snapshots":
        session_context = (((payload.get("snapshot") or {}).get("session_context")) if isinstance(payload.get("snapshot"), dict) else None) or {}
        if isinstance(session_context, dict):
            session_ts = _normalized_market_timestamp(session_context.get("timestamp"))
            if session_ts is not None:
                yield "payload.snapshot.session_context.timestamp", session_ts
    elif coll_name == "strategy_votes":
        vote = payload.get("vote") if isinstance(payload.get("vote"), dict) else {}
        nested_ts = _normalized_market_timestamp(vote.get("timestamp"))
        if nested_ts is not None:
            yield "payload.vote.timestamp", nested_ts
    elif coll_name == "trade_signals":
        signal = payload.get("signal") if isinstance(payload.get("signal"), dict) else {}
        nested_ts = _normalized_market_timestamp(signal.get("timestamp"))
        if nested_ts is not None:
            yield "payload.signal.timestamp", nested_ts
    elif coll_name == "strategy_positions":
        position = payload.get("position") if isinstance(payload.get("position"), dict) else {}
        nested_ts = _normalized_market_timestamp(position.get("timestamp"))
        if nested_ts is not None:
            yield "payload.position.timestamp", nested_ts


def migrate(*, dry_run: bool) -> dict[str, Any]:
    client = _mongo()
    db = client[str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"]
    summary: dict[str, Any] = {"dry_run": bool(dry_run), "collections": {}}
    for logical_name, coll_name in _collections().items():
        coll = db[coll_name]
        matched = 0
        modified = 0
        for doc in coll.find({}, {"_id": 1, "timestamp": 1, "received_at_ist": 1, "payload": 1, "trade_date_ist": 1, "market_time_ist": 1}):
            updates = dict(_iter_updates(doc, logical_name))
            if not updates:
                continue
            matched += 1
            if not dry_run:
                coll.update_one({"_id": doc["_id"]}, {"$set": updates})
                modified += 1
        summary["collections"][logical_name] = {"collection": coll_name, "matched": matched, "modified": modified}
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate live Mongo timestamps to IST string format.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(dry_run=bool(args.dry_run)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
