"""Redis topic consumer for snapshot events (Layer 3 -> Layer 4)."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
import uuid
from collections import deque
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
        dedupe_window_raw = str(os.getenv("STRATEGY_SNAPSHOT_DEDUPE_WINDOW") or "5000").strip()
        try:
            dedupe_window = int(dedupe_window_raw)
        except Exception:
            dedupe_window = 5000
        self._dedupe_window = max(100, dedupe_window)
        self._seen_snapshot_keys: set[str] = set()
        self._seen_snapshot_order: deque[str] = deque()
        lock_enabled_raw = str(os.getenv("STRATEGY_SINGLE_CONSUMER_LOCK_ENABLED") or "1").strip().lower()
        self._consumer_lock_enabled = lock_enabled_raw not in {"0", "false", "no", "off"}
        lock_ttl_raw = str(os.getenv("STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC") or "120").strip()
        try:
            lock_ttl_sec = int(lock_ttl_raw)
        except Exception:
            lock_ttl_sec = 120
        # Floor at 5s — refresh interval below adapts via TTL//2 so short TTLs
        # remain safe in production. The previous 30s floor blocked tests from
        # exercising the wait/retry path in reasonable time. 30s default is
        # still preserved when env var is unset.
        self._consumer_lock_ttl_sec = max(5, lock_ttl_sec)
        refresh_raw = str(os.getenv("STRATEGY_SINGLE_CONSUMER_LOCK_REFRESH_SEC") or "30").strip()
        try:
            refresh_sec = int(refresh_raw)
        except Exception:
            refresh_sec = 30
        self._consumer_lock_refresh_sec = max(5, min(refresh_sec, self._consumer_lock_ttl_sec // 2))
        self._consumer_lock_key = f"strategy_app:consumer_lock:{self.topic}"
        self._consumer_lock_owner = (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}:{self.topic}"
        )
        self._lock_refresh_failed = False

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

    def _acquire_consumer_lock(self) -> None:
        """Acquire the per-topic consumer lock with graceful handling of stale
        and contended states.

        Three branches:
          1. Lock free → SET NX succeeds → acquired immediately.
          2. Lock held by SAME hostname (our own previous process — dead, since
             we're a fresh PID) → atomically steal via CAS (EVAL). This is the
             container-restart case; container hostname is stable across docker
             restart so an exited PID's lock blocks the next start until TTL.
             Stealing avoids 30-60s of crash-loop noise.
          3. Lock held by DIFFERENT hostname (genuine concurrent consumer) →
             wait with backoff up to (TTL + 5s) for the lock to expire. If it
             persists past that, raise — there's a real duplicate that needs
             operator attention.

        The previous behaviour raised on the first failed SET NX regardless of
        owner, which made every container restart produce a noisy traceback
        that recovered only because docker's restart-policy retried.
        """
        if not self._consumer_lock_enabled:
            return
        if not hasattr(self._client, "set"):
            logger.warning("strategy consumer lock skipped: redis client does not support SET")
            return

        own_hostname = socket.gethostname()
        max_wait_sec = self._consumer_lock_ttl_sec + 5  # one TTL + small buffer
        wait_step_sec = max(2, min(10, self._consumer_lock_ttl_sec // 12))
        deadline = time.monotonic() + max_wait_sec
        attempt = 0

        while True:
            attempt += 1
            try:
                acquired = self._client.set(
                    self._consumer_lock_key,
                    self._consumer_lock_owner,
                    nx=True,
                    ex=self._consumer_lock_ttl_sec,
                )
            except Exception:
                logger.exception("strategy consumer lock acquire failed key=%s", self._consumer_lock_key)
                raise
            if acquired:
                logger.info(
                    "strategy consumer lock acquired key=%s ttl=%ss owner=%s attempt=%d",
                    self._consumer_lock_key,
                    self._consumer_lock_ttl_sec,
                    self._consumer_lock_owner,
                    attempt,
                )
                return

            existing_owner_raw = None
            if hasattr(self._client, "get"):
                try:
                    existing_owner_raw = self._client.get(self._consumer_lock_key)
                except Exception:
                    existing_owner_raw = None
            existing_owner = (
                existing_owner_raw.decode("utf-8", errors="replace")
                if isinstance(existing_owner_raw, (bytes, bytearray))
                else existing_owner_raw
            )
            existing_hostname = ""
            if isinstance(existing_owner, str) and ":" in existing_owner:
                existing_hostname = existing_owner.split(":", 1)[0]
            same_host = bool(existing_hostname) and existing_hostname == own_hostname

            if same_host:
                # Same hostname: this lock was held by a prior PID inside this
                # same container (now dead — we're a fresh start). Reclaim it
                # atomically: only steal if the value still matches what we saw,
                # so a genuine new owner from the same host (rare) wouldn't get
                # clobbered.
                stolen = False
                if hasattr(self._client, "eval"):
                    try:
                        result = self._client.eval(
                            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                            "return redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]) "
                            "else return nil end",
                            1,
                            self._consumer_lock_key,
                            existing_owner,
                            self._consumer_lock_owner,
                            self._consumer_lock_ttl_sec,
                        )
                        stolen = bool(result)
                    except Exception:
                        logger.exception(
                            "stale lock reclaim (CAS) failed key=%s; will fall back to wait loop",
                            self._consumer_lock_key,
                        )
                else:
                    # No EVAL support (test fakes etc.) — best effort: delete + retry SETNX
                    try:
                        if hasattr(self._client, "delete"):
                            self._client.delete(self._consumer_lock_key)
                    except Exception:
                        pass
                if stolen:
                    logger.info(
                        "strategy consumer lock reclaimed from prior same-host process "
                        "key=%s prior_owner=%s new_owner=%s",
                        self._consumer_lock_key,
                        existing_owner,
                        self._consumer_lock_owner,
                    )
                    return
                # CAS failed (someone else took it between GET and EVAL) — loop will retry
                logger.info(
                    "stale-lock reclaim attempt did not commit, will retry (existing_owner=%s)",
                    existing_owner,
                )

            now = time.monotonic()
            if now >= deadline:
                raise RuntimeError(
                    "duplicate strategy consumer detected after waiting "
                    f"{max_wait_sec}s for topic={self.topic} lock_key={self._consumer_lock_key} "
                    f"existing_owner={existing_owner!r} (attempts={attempt})"
                )

            remaining = max(0, int(deadline - now))
            logger.warning(
                "strategy consumer lock contended (%s host); existing_owner=%s — "
                "retry in %ds (attempt %d, %ds before timeout)",
                "same" if same_host else "different",
                existing_owner,
                wait_step_sec,
                attempt,
                remaining,
            )
            # _stop_event-aware sleep so a shutdown during contention exits cleanly
            if self._stop_event.wait(wait_step_sec):
                logger.info("stop requested while waiting on consumer lock; aborting acquire")
                return

    def _refresh_consumer_lock(self) -> None:
        if not self._consumer_lock_enabled:
            return
        if not all(hasattr(self._client, method) for method in ("get", "expire")):
            return
        key = self._consumer_lock_key
        owner = self._consumer_lock_owner
        ttl = int(self._consumer_lock_ttl_sec)
        try:
            # Atomic when EVAL is available; otherwise fallback to best-effort get+expire.
            if hasattr(self._client, "eval"):
                refreshed = self._client.eval(
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                    "return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end",
                    1,
                    key,
                    owner,
                    ttl,
                )
                ok = bool(int(refreshed or 0))
            else:
                current_owner = self._client.get(key)
                if current_owner != owner:
                    ok = False
                else:
                    ok = bool(self._client.expire(key, ttl))
            if not ok:
                self._lock_refresh_failed = True
                raise RuntimeError(
                    "strategy consumer lock lost for "
                    f"topic={self.topic} key={self._consumer_lock_key}"
                )
        except Exception:
            logger.exception("strategy consumer lock refresh failed key=%s", self._consumer_lock_key)
            raise

    def _release_consumer_lock(self) -> None:
        if not self._consumer_lock_enabled:
            return
        if not hasattr(self._client, "get"):
            return
        key = self._consumer_lock_key
        owner = self._consumer_lock_owner
        try:
            deleted = False
            if hasattr(self._client, "eval"):
                removed = self._client.eval(
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                    "return redis.call('DEL', KEYS[1]) else return 0 end",
                    1,
                    key,
                    owner,
                )
                deleted = bool(int(removed or 0))
            elif hasattr(self._client, "delete"):
                current_owner = self._client.get(key)
                if current_owner == owner:
                    deleted = bool(self._client.delete(key))
            if deleted:
                logger.info("strategy consumer lock released key=%s owner=%s", key, owner)
        except Exception:
            logger.exception("strategy consumer lock release failed key=%s", key)

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

    def start(self, *, max_events: Optional[int] = None) -> int:
        """Blocking consume loop. Returns consumed event count."""
        max_count = None if max_events is None else max(0, int(max_events))
        self._acquire_consumer_lock()
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(self.topic)
        self._running = True
        self._stop_event.clear()
        self._lock_refresh_failed = False
        last_lock_refresh_at = time.monotonic()
        logger.info("strategy consumer subscribed topic=%s", self.topic)

        try:
            while not self._stop_event.is_set():
                if max_count is not None and self._events_seen >= max_count:
                    break
                now = time.monotonic()
                if now - last_lock_refresh_at >= self._consumer_lock_refresh_sec:
                    self._refresh_consumer_lock()
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

                snapshot = event.get("snapshot")
                if not isinstance(snapshot, dict):
                    continue

                self._handle_session(snapshot)
                if not self._accept_snapshot_once(event, snapshot):
                    logger.debug(
                        "strategy consumer skipped duplicate snapshot snapshot_id=%s run_id=%s",
                        str(event.get("snapshot_id") or ""),
                        _event_run_id(event) or "",
                    )
                    continue
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
            self._release_consumer_lock()
            logger.info("strategy consumer stopped topic=%s events=%s", self.topic, self._events_seen)

        return self._events_seen
