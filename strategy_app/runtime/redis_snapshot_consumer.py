"""Redis topic consumer for snapshot events (Layer 3 -> Layer 4)."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from threading import Event
from typing import Callable, Optional

import redis

from contracts_app import parse_snapshot_event, redis_connection_kwargs, snapshot_topic

from ..contracts import StrategyEngine, TradeSignal

logger = logging.getLogger(__name__)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


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

    raw_ts = str(session_context.get("timestamp") or snapshot.get("timestamp") or "").strip()
    if len(raw_ts) >= 10:
        return _parse_trade_date(raw_ts[:10])
    return None


class RedisSnapshotConsumer:
    """Subscribe to snapshot events and invoke the strategy engine contract."""

    def __init__(
        self,
        *,
        engine: StrategyEngine,
        topic: Optional[str] = None,
        client: Optional[redis.Redis] = None,
        poll_interval_sec: float = 0.2,
        on_signal: Optional[Callable[[TradeSignal], None]] = None,
    ) -> None:
        self.engine = engine
        self.topic = str(topic or snapshot_topic()).strip() or snapshot_topic()
        self._client = client or _redis_client()
        self._poll_interval_sec = max(0.01, float(poll_interval_sec))
        self._on_signal = on_signal
        self._stop_event = Event()
        self._running = False
        self._current_session: Optional[date] = None
        self._events_seen = 0

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
            return
        if session_day != self._current_session:
            prev = self._current_session
            self.engine.on_session_end(prev)
            self.engine.on_session_start(session_day)
            self._current_session = session_day

    def start(self, *, max_events: Optional[int] = None) -> int:
        """Blocking consume loop. Returns consumed event count."""
        max_count = None if max_events is None else max(0, int(max_events))
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(self.topic)
        self._running = True
        self._stop_event.clear()
        logger.info("strategy consumer subscribed topic=%s", self.topic)

        try:
            while not self._stop_event.is_set():
                if max_count is not None and self._events_seen >= max_count:
                    break

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

                snapshot = event.get("snapshot")
                if not isinstance(snapshot, dict):
                    continue

                self._handle_session(snapshot)
                signal = self.engine.evaluate(snapshot)
                if signal is not None and self._on_signal is not None:
                    self._on_signal(signal)

                self._events_seen += 1
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
            logger.info("strategy consumer stopped topic=%s events=%s", self.topic, self._events_seen)

        return self._events_seen
