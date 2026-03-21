from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from contracts_app import (
    TimestampSourceMode,
    normalize_decision_mode,
    normalize_engine_mode,
    normalize_reason_code,
    parse_snapshot_event,
    parse_strategy_position_event,
    parse_strategy_vote_event,
    parse_trade_signal_event,
)
from .time_utils import IST, parse_market_timestamp_ist, to_ist, to_ist_iso

try:
    from pymongo import ASCENDING, MongoClient
except Exception:  # pragma: no cover
    ASCENDING = 1
    MongoClient = None

def _parse_ts(value: Any) -> Optional[datetime]:
    return parse_market_timestamp_ist(value)


def _parse_legacy_mongo_ts(value: Any) -> Optional[datetime]:
    return parse_market_timestamp_ist(value, naive_mode=TimestampSourceMode.LEGACY_MONGO_UTC)


def _resolve_run_id(event: dict[str, Any], body: dict[str, Any]) -> Optional[str]:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        text = str(metadata.get("run_id") or "").strip()
        if text:
            return text
    text = str(body.get("run_id") or "").strip()
    return text or None


def _resolve_metadata_text(event: dict[str, Any], key: str) -> Optional[str]:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return None
    text = str(metadata.get(key) or "").strip()
    return text or None


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _optional_engine_mode(value: Any) -> Optional[str]:
    return normalize_engine_mode(value)


def _optional_decision_mode(value: Any) -> Optional[str]:
    return normalize_decision_mode(value)


def _optional_reason_code(value: Any) -> Optional[str]:
    return normalize_reason_code(value)


def _optional_metrics(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key, raw in value.items():
        if raw is None:
            continue
        out[str(key)] = raw
    return out or None


def _optional_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return float(parsed)


def _flatten_ml_metrics(value: Any) -> dict[str, Optional[float]]:
    metrics = value if isinstance(value, dict) else {}
    direction_up = _optional_float(metrics.get("direction_up_prob"))
    ce_prob = _optional_float(metrics.get("ce_prob"))
    pe_prob = _optional_float(metrics.get("pe_prob"))
    if ce_prob is None and direction_up is not None:
        ce_prob = float(direction_up)
    if pe_prob is None and direction_up is not None:
        pe_prob = float(1.0 - direction_up)
    return {
        "ml_entry_prob": _optional_float(metrics.get("entry_prob")),
        "ml_direction_up_prob": direction_up,
        "ml_ce_prob": ce_prob,
        "ml_pe_prob": pe_prob,
        "ml_recipe_prob": _optional_float(metrics.get("recipe_prob")),
        "ml_recipe_margin": _optional_float(metrics.get("recipe_margin")),
    }


def _actual_outcome_from_position(position: dict[str, Any]) -> Optional[str]:
    exit_reason = str(position.get("exit_reason") or "").strip().upper()
    pnl_pct = _optional_float(position.get("pnl_pct"))
    if exit_reason in {"STOP_LOSS", "TRAILING_STOP", "RISK_BREACH"}:
        return "stop"
    if exit_reason == "TIME_STOP":
        return "time"
    if pnl_pct is not None:
        if pnl_pct > 0:
            return "win"
        if pnl_pct < 0:
            return "loss"
    return "unknown" if exit_reason or pnl_pct is not None else None


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
            coll.create_index("received_at_ttl", expireAfterSeconds=int(ttl_days * 24 * 60 * 60))
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
            "timestamp": to_ist_iso(ts_ist),
            "trade_date_ist": ts_ist.date().isoformat(),
            "market_time_ist": ts_ist.strftime("%H:%M:%S"),
            "received_at_ist": to_ist_iso(datetime.now(tz=IST)),
            "received_at_ttl": datetime.now(tz=IST),
            "payload": event,
        }
        db[self.collection_name].insert_one(doc)
        return True


class StrategyMongoWriter:
    def __init__(self) -> None:
        self.vote_collection_name = str(os.getenv("MONGO_COLL_STRATEGY_VOTES") or "strategy_votes")
        self.signal_collection_name = str(os.getenv("MONGO_COLL_TRADE_SIGNALS") or "trade_signals")
        self.position_collection_name = str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS") or "strategy_positions")
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
        vote_coll = self._db[self.vote_collection_name]
        signal_coll = self._db[self.signal_collection_name]
        position_coll = self._db[self.position_collection_name]

        vote_coll.create_index([("strategy", ASCENDING), ("trade_date_ist", ASCENDING), ("timestamp", ASCENDING)])
        vote_coll.create_index([("snapshot_id", ASCENDING), ("strategy", ASCENDING)])
        vote_coll.create_index([("run_id", ASCENDING), ("trade_date_ist", ASCENDING), ("timestamp", ASCENDING)])
        vote_coll.create_index([("trade_date_ist", ASCENDING), ("engine_mode", ASCENDING), ("timestamp", ASCENDING)])
        signal_coll.create_index([("signal_id", ASCENDING)], unique=True)
        signal_coll.create_index([("trade_date_ist", ASCENDING), ("signal_type", ASCENDING), ("timestamp", ASCENDING)])
        signal_coll.create_index([("run_id", ASCENDING), ("trade_date_ist", ASCENDING), ("timestamp", ASCENDING)])
        signal_coll.create_index([("trade_date_ist", ASCENDING), ("engine_mode", ASCENDING), ("timestamp", ASCENDING)])
        position_coll.create_index([("position_id", ASCENDING), ("event", ASCENDING), ("timestamp", ASCENDING)])
        position_coll.create_index([("trade_date_ist", ASCENDING), ("timestamp", ASCENDING)])
        position_coll.create_index([("run_id", ASCENDING), ("trade_date_ist", ASCENDING), ("timestamp", ASCENDING)])

        ttl_days = int(os.getenv("MONGO_PERSIST_TTL_DAYS") or "0")
        if ttl_days > 0:
            ttl_seconds = int(ttl_days * 24 * 60 * 60)
            vote_coll.create_index("received_at_ttl", expireAfterSeconds=ttl_seconds)
            signal_coll.create_index("received_at_ttl", expireAfterSeconds=ttl_seconds)
            position_coll.create_index("received_at_ttl", expireAfterSeconds=ttl_seconds)
        self._indexes_ready = True

    def write_strategy_event(self, payload: dict[str, Any]) -> bool:
        return (
            self.write_strategy_vote_event(payload)
            or self.write_trade_signal_event(payload)
            or self.write_strategy_position_event(payload)
        )

    def write_strategy_vote_event(self, payload: dict[str, Any]) -> bool:
        event = parse_strategy_vote_event(payload)
        if event is None:
            return False
        db = self._db_handle()
        if db is None:
            return False

        vote = event.get("vote") if isinstance(event.get("vote"), dict) else {}
        run_id = _resolve_run_id(event, vote)
        ts = _parse_ts(vote.get("timestamp")) or _parse_ts(event.get("published_at")) or datetime.now(tz=IST)
        ts_ist = to_ist(ts)
        doc = {
            "event_type": "strategy_vote",
            "event_version": str(event.get("event_version") or "1.0"),
            "source": str(event.get("source") or "strategy_app"),
            "event_id": str(event.get("event_id") or ""),
            "snapshot_id": str(vote.get("snapshot_id") or ""),
            "strategy": str(vote.get("strategy") or "").strip().upper(),
            "regime": str(vote.get("regime") or ""),
            "regime_conf": vote.get("regime_conf"),
            "signal_type": str(vote.get("signal_type") or ""),
            "direction": str(vote.get("direction") or ""),
            "timestamp": to_ist_iso(ts_ist),
            "trade_date_ist": str(vote.get("trade_date") or ts_ist.date().isoformat()),
            "market_time_ist": ts_ist.strftime("%H:%M:%S"),
            "received_at_ist": to_ist_iso(datetime.now(tz=IST)),
            "received_at_ttl": datetime.now(tz=IST),
            "run_id": run_id,
            "confidence": vote.get("confidence"),
            "reason": vote.get("reason"),
            "engine_mode": _optional_engine_mode(vote.get("engine_mode")),
            "decision_mode": _optional_decision_mode(vote.get("decision_mode")),
            "decision_reason_code": _optional_reason_code(vote.get("decision_reason_code")),
            "decision_metrics": _optional_metrics(vote.get("decision_metrics")),
            **_flatten_ml_metrics(vote.get("decision_metrics")),
            "strategy_family_version": _optional_text(vote.get("strategy_family_version")),
            "strategy_profile_id": _optional_text(vote.get("strategy_profile_id")),
            "payload": event,
        }
        db[self.vote_collection_name].insert_one(doc)
        return True

    def write_trade_signal_event(self, payload: dict[str, Any]) -> bool:
        event = parse_trade_signal_event(payload)
        if event is None:
            return False
        db = self._db_handle()
        if db is None:
            return False

        signal = event.get("signal") if isinstance(event.get("signal"), dict) else {}
        run_id = _resolve_run_id(event, signal)
        ts = _parse_ts(signal.get("timestamp")) or _parse_ts(event.get("published_at")) or datetime.now(tz=IST)
        ts_ist = to_ist(ts)
        doc = {
            "event_type": "trade_signal",
            "event_version": str(event.get("event_version") or "1.0"),
            "source": str(event.get("source") or "strategy_app"),
            "event_id": str(event.get("event_id") or ""),
            "signal_id": str(signal.get("signal_id") or ""),
            "snapshot_id": str(signal.get("snapshot_id") or ""),
            "regime": str(signal.get("regime") or ""),
            "regime_conf": signal.get("regime_conf"),
            "signal_type": str(signal.get("signal_type") or ""),
            "direction": str(signal.get("direction") or ""),
            "timestamp": to_ist_iso(ts_ist),
            "trade_date_ist": ts_ist.date().isoformat(),
            "market_time_ist": ts_ist.strftime("%H:%M:%S"),
            "received_at_ist": to_ist_iso(datetime.now(tz=IST)),
            "received_at_ttl": datetime.now(tz=IST),
            "run_id": run_id,
            "position_id": signal.get("position_id"),
            "confidence": signal.get("confidence"),
            "reason": signal.get("reason"),
            "engine_mode": _optional_engine_mode(signal.get("engine_mode")),
            "decision_mode": _optional_decision_mode(signal.get("decision_mode")),
            "decision_reason_code": _optional_reason_code(signal.get("decision_reason_code")),
            "decision_metrics": _optional_metrics(signal.get("decision_metrics")),
            **_flatten_ml_metrics(signal.get("decision_metrics")),
            "strategy_family_version": _optional_text(signal.get("strategy_family_version")),
            "strategy_profile_id": _optional_text(signal.get("strategy_profile_id")),
            "payload": event,
        }
        db[self.signal_collection_name].insert_one(doc)
        return True

    def write_strategy_position_event(self, payload: dict[str, Any]) -> bool:
        event = parse_strategy_position_event(payload)
        if event is None:
            return False
        db = self._db_handle()
        if db is None:
            return False

        position = event.get("position") if isinstance(event.get("position"), dict) else {}
        run_id = _resolve_run_id(event, position)
        ts = _parse_ts(position.get("timestamp")) or _parse_ts(event.get("published_at")) or datetime.now(tz=IST)
        ts_ist = to_ist(ts)
        actual_outcome = _actual_outcome_from_position(position) if str(position.get("event") or "").strip().upper() == "POSITION_CLOSE" else None
        actual_return_pct = _optional_float(position.get("pnl_pct")) if actual_outcome is not None else None
        doc = {
            "event_type": "strategy_position",
            "event_version": str(event.get("event_version") or "1.0"),
            "source": str(event.get("source") or "strategy_app"),
            "event_id": str(event.get("event_id") or ""),
            "position_id": str(position.get("position_id") or ""),
            "signal_id": _optional_text(position.get("signal_id")) or _resolve_metadata_text(event, "signal_id"),
            "event": str(position.get("event") or ""),
            "timestamp": to_ist_iso(ts_ist),
            "trade_date_ist": ts_ist.date().isoformat(),
            "market_time_ist": ts_ist.strftime("%H:%M:%S"),
            "received_at_ist": to_ist_iso(datetime.now(tz=IST)),
            "received_at_ttl": datetime.now(tz=IST),
            "run_id": run_id,
            "direction": position.get("direction"),
            "strike": position.get("strike"),
            "reason": position.get("reason"),
            "engine_mode": _optional_engine_mode(position.get("engine_mode")),
            "decision_mode": _optional_decision_mode(position.get("decision_mode")),
            "decision_reason_code": _optional_reason_code(position.get("decision_reason_code")),
            "decision_metrics": _optional_metrics(position.get("decision_metrics")),
            **_flatten_ml_metrics(position.get("decision_metrics")),
            "actual_outcome": actual_outcome,
            "actual_return_pct": actual_return_pct,
            "strategy_family_version": _optional_text(position.get("strategy_family_version")),
            "strategy_profile_id": _optional_text(position.get("strategy_profile_id")),
            "payload": event,
        }
        db[self.position_collection_name].insert_one(doc)
        return True
