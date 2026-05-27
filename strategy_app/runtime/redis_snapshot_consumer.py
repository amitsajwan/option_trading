"""Redis topic consumer for snapshot events (Layer 3 -> Layer 4)."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from collections import deque
from datetime import date
from threading import Event
from typing import Any, Callable, Mapping, Optional

import redis

from contracts_app import build_snapshot_event, parse_snapshot_event, redis_connection_kwargs, snapshot_topic

from ..contracts import StrategyEngine, TradeSignal
from .consumer_lock import ConsumerLock, lock_config_from_env

logger = logging.getLogger(__name__)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
STREAM_GROUP_NAME = "consumer-group-1"
SENTINEL_TYPE = "sentinel"


def _redis_client() -> redis.Redis:
    return redis.Redis(**redis_connection_kwargs(decode_responses=True, for_pubsub=True))


def _parse_trade_date(raw: str) -> Optional[date]:
    """
    Parse strict YYYY-MM-DD only.
    Strategy session boundaries are IST day boundaries from snapshot payload.
    """
    text = str(raw or "").strip()
    match = _DATE_RE.match(text)
    if not match:
        return None
    try:
        yyyy, mm, dd = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return date(yyyy, mm, dd)
    except Exception:
        return None


def _snapshot_trade_date(snapshot: dict) -> Optional[date]:
    session_context = snapshot.get("session_context") if isinstance(snapshot.get("session_context"), dict) else {}
    raw_date = str(session_context.get("date") or "").strip()
    if raw_date:
        parsed = _parse_trade_date(raw_date)
        if parsed is not None:
            return parsed

    top_level_date = str(snapshot.get("trade_date") or "").strip()
    if top_level_date:
        parsed = _parse_trade_date(top_level_date)
        if parsed is not None:
            return parsed

    raw_ts = str(session_context.get("timestamp") or snapshot.get("timestamp") or "").strip()
    if len(raw_ts) >= 10:
        return _parse_trade_date(raw_ts[:10])
    return None


def _event_metadata(event: dict[str, object]) -> dict[str, object]:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return {}


def _event_run_id(event: dict[str, object]) -> Optional[str]:
    metadata = _event_metadata(event)
    text = str(metadata.get("run_id") or "").strip()
    if text:
        return text
    return None


def _decode_stream_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def _decode_stream_fields(fields: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(_decode_stream_value(k)): _decode_stream_value(v) for k, v in dict(fields or {}).items()}


def _json_dict(raw: Any) -> Optional[dict[str, Any]]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _merge_stream_metadata(event: dict[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    merged = dict(metadata)
    for key in ("source_mode", "run_id", "sim_label"):
        value = fields.get(key)
        if value not in (None, ""):
            merged[key] = value
    event["metadata"] = merged
    return event


def _snapshot_event_from_stream_fields(fields: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Normalize SIM-3 Redis Stream fields into the pubsub snapshot envelope."""
    payload = _json_dict(fields.get("payload"))
    if payload is None:
        return None

    direct = parse_snapshot_event(payload)
    if direct is not None:
        return _merge_stream_metadata(dict(direct), fields)

    nested_payload = payload.get("payload")
    if isinstance(nested_payload, dict):
        nested_event = parse_snapshot_event(nested_payload)
        if nested_event is not None:
            return _merge_stream_metadata(dict(nested_event), fields)
        snapshot = nested_payload.get("snapshot")
        if isinstance(snapshot, dict):
            event = build_snapshot_event(
                snapshot=snapshot,
                source=str(payload.get("source") or fields.get("source_mode") or "sim_publisher"),
                event_id=str(payload.get("event_id") or ""),
                published_at=str(payload.get("published_at") or payload.get("timestamp") or ""),
                metadata=payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
            )
            return _merge_stream_metadata(event, fields)

    snapshot = payload.get("snapshot")
    if isinstance(snapshot, dict):
        event = build_snapshot_event(
            snapshot=snapshot,
            source=str(payload.get("source") or fields.get("source_mode") or "sim_publisher"),
            event_id=str(payload.get("event_id") or ""),
            published_at=str(payload.get("published_at") or payload.get("timestamp") or ""),
            metadata=payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
        )
        return _merge_stream_metadata(event, fields)
    return None


class RedisSnapshotConsumer:
    """Subscribe to snapshot events and invoke the strategy engine contract."""

    def __init__(
        self,
        *,
        engine: StrategyEngine,
        topic: Optional[str] = None,
        client: Optional[redis.Redis] = None,
        transport: Optional[str] = None,
        stream_name: Optional[str] = None,
        stream_group: str = STREAM_GROUP_NAME,
        stream_consumer_name: Optional[str] = None,
        poll_interval_sec: float = 0.2,
        on_signal: Optional[Callable[[TradeSignal], None]] = None,
    ) -> None:
        self.engine = engine
        self.topic = str(topic or snapshot_topic()).strip() or snapshot_topic()
        self._client = client or _redis_client()
        env_transport = str(os.getenv("STRATEGY_CONSUMER_TRANSPORT") or "pubsub").strip().lower()
        self._transport = str(transport or env_transport or "pubsub").strip().lower()
        if self._transport not in {"pubsub", "streams"}:
            raise ValueError("transport must be 'pubsub' or 'streams'")
        self._stream_name = str(stream_name or os.getenv("STRATEGY_STREAM_NAME") or "").strip()
        self._stream_group = str(stream_group or STREAM_GROUP_NAME).strip() or STREAM_GROUP_NAME
        self._stream_consumer_name = str(stream_consumer_name or f"consumer-{socket.gethostname()}").strip()
        self._poll_interval_sec = max(0.01, float(poll_interval_sec))
        self._on_signal = on_signal
        self._stop_event = Event()
        self._running = False
        self._current_session: Optional[date] = None
        self._events_seen = 0
        dedupe_window_raw = str(os.getenv("STRATEGY_SNAPSHOT_DEDUPE_WINDOW") or "5000").strip()
        try:
            dedupe_window = int(dedupe_window_raw)
        except Exception:
            dedupe_window = 5000
        self._dedupe_window = max(100, dedupe_window)
        self._seen_snapshot_keys: set[str] = set()
        self._seen_snapshot_order: deque[str] = deque()
        lock_enabled, lock_ttl_sec, lock_refresh_sec, _lock_max_wait = lock_config_from_env()
        self._consumer_lock = ConsumerLock(
            self._client,
            topic=self.topic,
            stop_event=self._stop_event,
            enabled=lock_enabled,
            ttl_sec=lock_ttl_sec,
            refresh_sec=lock_refresh_sec,
        )

    def stop(self) -> None:
        self._stop_event.set()

    def is_running(self) -> bool:
        return bool(self._running and not self._stop_event.is_set())

    def _handle_session(self, snapshot: dict) -> None:
        session_day = _snapshot_trade_date(snapshot)
        if session_day is None:
            return
        if self._current_session is None:
            self.engine.on_session_start(session_day)
            self._current_session = session_day
            self._seen_snapshot_keys.clear()
            self._seen_snapshot_order.clear()
            return
        if session_day != self._current_session:
            prev = self._current_session
            try:
                if prev is not None:
                    try:
                        self.engine.on_session_end(prev)
                    except Exception:
                        logger.exception("session end hook failed prev=%s", prev.isoformat())
            finally:
                try:
                    self.engine.on_session_start(session_day)
                    self._current_session = session_day
                except Exception:
                    logger.exception("session start hook failed session=%s", session_day.isoformat())
                self._seen_snapshot_keys.clear()
                self._seen_snapshot_order.clear()

    def _snapshot_process_key(self, event: dict[str, object], snapshot: dict[str, object]) -> str:
        run_id = _event_run_id(event) or ""
        snapshot_id = str(event.get("snapshot_id") or snapshot.get("snapshot_id") or "").strip()
        return f"{run_id}:{snapshot_id}"

    def _accept_snapshot_once(self, event: dict[str, object], snapshot: dict[str, object]) -> bool:
        key = self._snapshot_process_key(event, snapshot)
        if not key or key in self._seen_snapshot_keys:
            return False
        self._seen_snapshot_keys.add(key)
        self._seen_snapshot_order.append(key)
        while len(self._seen_snapshot_order) > self._dedupe_window:
            evicted = self._seen_snapshot_order.popleft()
            self._seen_snapshot_keys.discard(evicted)
        return True

    def _process_event(self, event: dict[str, object]) -> bool:
        snapshot = event.get("snapshot")
        if not isinstance(snapshot, dict):
            return False

        self._handle_session(snapshot)
        if not self._accept_snapshot_once(event, snapshot):
            logger.debug(
                "strategy consumer skipped duplicate snapshot snapshot_id=%s run_id=%s",
                str(event.get("snapshot_id") or ""),
                _event_run_id(event) or "",
            )
            return False
        run_id = _event_run_id(event)
        metadata = _event_metadata(event)
        if hasattr(self.engine, "set_run_context"):
            try:
                self.engine.set_run_context(run_id, metadata)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("failed to set strategy run context run_id=%s", run_id)
        signal = self.engine.evaluate(snapshot)
        if signal is not None and self._on_signal is not None:
            self._on_signal(signal)
        self._events_seen += 1
        return True

    def _ensure_stream_group(self) -> None:
        try:
            self._client.xgroup_create(
                self._stream_name,
                self._stream_group,
                id="0",
                mkstream=True,
            )
            logger.info(
                "strategy stream consumer group created stream=%s group=%s",
                self._stream_name,
                self._stream_group,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def _read_stream_batch(self, stream_id: str) -> list[tuple[str, dict[str, Any]]]:
        response = self._client.xreadgroup(
            self._stream_group,
            self._stream_consumer_name,
            {self._stream_name: stream_id},
            count=50,
            block=5000,
        )
        out: list[tuple[str, dict[str, Any]]] = []
        for _stream_name, entries in response or []:
            for entry_id, fields in entries or []:
                out.append((str(_decode_stream_value(entry_id)), _decode_stream_fields(fields)))
        return out

    def _start_streams(self, *, max_events: Optional[int]) -> int:
        if not self._stream_name:
            raise ValueError("STRATEGY_STREAM_NAME is required when STRATEGY_CONSUMER_TRANSPORT=streams")
        self._ensure_stream_group()
        self._running = True
        self._stop_event.clear()
        logger.info(
            "strategy consumer stream started stream=%s group=%s consumer=%s",
            self._stream_name,
            self._stream_group,
            self._stream_consumer_name,
        )
        read_pending = True
        try:
            while not self._stop_event.is_set():
                if max_events is not None and self._events_seen >= max_events:
                    break
                batch = self._read_stream_batch("0" if read_pending else ">")
                if read_pending and not batch:
                    read_pending = False
                    continue
                if not batch:
                    time.sleep(self._poll_interval_sec)
                    continue
                for entry_id, fields in batch:
                    if str(fields.get("type") or "").lower() == SENTINEL_TYPE:
                        logger.info(
                            "strategy consumer received sentinel stream=%s run_id=%s aborted=%s total_published=%s",
                            self._stream_name,
                            fields.get("run_id"),
                            fields.get("aborted"),
                            fields.get("total_published"),
                        )
                        self._stop_event.set()
                        break
                    event = _snapshot_event_from_stream_fields(fields)
                    if event is None:
                        logger.warning("ignored invalid stream snapshot entry stream=%s id=%s", self._stream_name, entry_id)
                        self._client.xack(self._stream_name, self._stream_group, entry_id)
                        continue
                    self._process_event(event)
                    self._client.xack(self._stream_name, self._stream_group, entry_id)
                    if max_events is not None and self._events_seen >= max_events:
                        break
        finally:
            if self._current_session is not None:
                try:
                    self.engine.on_session_end(self._current_session)
                except Exception:
                    logger.exception("session end hook failed")
                self._current_session = None
            self._running = False
            logger.info("strategy consumer stream stopped stream=%s events=%s", self._stream_name, self._events_seen)

        return self._events_seen

    def start(self, *, max_events: Optional[int] = None) -> int:
        """Blocking consume loop. Returns consumed event count."""
        max_count = None if max_events is None else max(0, int(max_events))
        if self._transport == "streams":
            return self._start_streams(max_events=max_count)
        self._consumer_lock.acquire()
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(self.topic)
        self._running = True
        self._stop_event.clear()
        last_lock_refresh_at = time.monotonic()
        logger.info("strategy consumer subscribed topic=%s", self.topic)

        try:
            while not self._stop_event.is_set():
                if max_count is not None and self._events_seen >= max_count:
                    break
                now = time.monotonic()
                if now - last_lock_refresh_at >= self._consumer_lock.refresh_interval_sec:
                    self._consumer_lock.refresh()
                    last_lock_refresh_at = now

                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not msg:
                    time.sleep(self._poll_interval_sec)
                    continue

                data = msg.get("data")
                if not isinstance(data, str):
                    continue
                try:
                    payload = json.loads(data)
                except Exception:
                    logger.warning("ignored non-json event on topic=%s", self.topic)
                    continue

                event = parse_snapshot_event(payload)
                if event is None:
                    continue

                self._process_event(event)
        finally:
            try:
                pubsub.close()
            except Exception:
                pass
            if self._current_session is not None:
                try:
                    self.engine.on_session_end(self._current_session)
                except Exception:
                    logger.exception("session end hook failed")
                self._current_session = None
            self._running = False
            self._consumer_lock.release()
            logger.info("strategy consumer stopped topic=%s events=%s", self.topic, self._events_seen)

        return self._events_seen
