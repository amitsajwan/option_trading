"""Shared Redis pub/sub reader pool for WebSocket connections.

Replaces the per-connection Thread model (C1 — arch/streams-loose-coupling).

Design:
  - One background thread owns one Redis connection + one pubsub object.
  - Reverse indexes (channel → conn_ids, pattern → conn_ids) allow O(1)
    dispatch lookup and shared Redis subscribe/unsubscribe: Redis receives
    one SUBSCRIBE per unique channel across all browser tabs, not one per tab.
    When the last listener on a channel disconnects, Redis is UNSUBSCRIBEd.
  - Each WebSocket connection gets an asyncio.Queue(maxsize=100).
    When full, the oldest message is dropped (display-only feeds).
  - asyncio.Queue is NOT thread-safe: all puts go via loop.call_soon_threadsafe.

Thread safety:
  - All mutable state (_connections, _channel_to_conns, _pattern_to_conns)
    is guarded by a single _lock.
  - The reader thread never mutates the registry; it only dispatches via
    call_soon_threadsafe into each connection's event loop.

Lifecycle:
  - Reader thread starts lazily on first register() call, restarts on crash.
  - Thread is a daemon; it exits when the process exits.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import queue
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

import redis

logger = logging.getLogger(__name__)

MAX_POOL_THREADS = 1  # single shared reader thread — kept for API compat
_QUEUE_MAXSIZE = 100


class SharedRedisPool:
    """Single-thread shared Redis pub/sub pool for all WebSocket connections."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # conn_id -> {"queue": asyncio.Queue, "loop": event_loop,
        #              "channels": set[str], "patterns": set[str]}
        self._connections: Dict[str, Dict[str, Any]] = {}
        # Reverse indexes for O(1) dispatch and shared unsubscribe
        self._channel_to_conns: Dict[str, Set[str]] = {}
        self._pattern_to_conns: Dict[str, Set[str]] = {}

        self._redis_client: Optional[redis.Redis] = None
        self._pubsub: Optional[Any] = None
        self._ctrl_q: "queue.SimpleQueue[Tuple[str, str]]" = queue.SimpleQueue()
        self._thread: Optional[threading.Thread] = None

        self._host = "localhost"
        self._port = 6379

    def configure(self, host: str, port: int) -> None:
        self._host = host
        self._port = port

    # ------------------------------------------------------------------
    # Public API (called from async WS handler, runs in the event loop)
    # ------------------------------------------------------------------

    def register(self, conn_id: str, loop: asyncio.AbstractEventLoop) -> "asyncio.Queue[Any]":
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        with self._lock:
            self._connections[conn_id] = {
                "queue": q,
                "loop": loop,
                "channels": set(),
                "patterns": set(),
            }
        self._ensure_thread()
        logger.debug("ws-pool register conn=%s", conn_id)
        return q

    def unregister(self, conn_id: str) -> None:
        with self._lock:
            conn = self._connections.pop(conn_id, None)
            if not conn:
                return
            for ch in conn.get("channels", set()):
                listeners = self._channel_to_conns.get(ch)
                if listeners is not None:
                    listeners.discard(conn_id)
                    if not listeners:
                        self._channel_to_conns.pop(ch, None)
                        self._ctrl_q.put(("unsubscribe", ch))
            for pat in conn.get("patterns", set()):
                listeners = self._pattern_to_conns.get(pat)
                if listeners is not None:
                    listeners.discard(conn_id)
                    if not listeners:
                        self._pattern_to_conns.pop(pat, None)
                        self._ctrl_q.put(("punsubscribe", pat))
        logger.debug("ws-pool unregister conn=%s", conn_id)

    def subscribe(self, conn_id: str, kind: str, name: str) -> None:
        with self._lock:
            conn = self._connections.get(conn_id)
            if not conn:
                return
            if kind == "pattern":
                conn["patterns"].add(name)
                listeners = self._pattern_to_conns.setdefault(name, set())
                if not listeners:
                    self._ctrl_q.put(("psubscribe", name))
                listeners.add(conn_id)
            else:
                conn["channels"].add(name)
                listeners = self._channel_to_conns.setdefault(name, set())
                if not listeners:
                    self._ctrl_q.put(("subscribe", name))
                listeners.add(conn_id)

    def unsubscribe(self, conn_id: str, kind: str, name: str) -> None:
        with self._lock:
            conn = self._connections.get(conn_id)
            if not conn:
                return
            if kind == "pattern":
                conn["patterns"].discard(name)
                listeners = self._pattern_to_conns.get(name, set())
                listeners.discard(conn_id)
                if not listeners:
                    self._pattern_to_conns.pop(name, None)
                    self._ctrl_q.put(("punsubscribe", name))
            else:
                conn["channels"].discard(name)
                listeners = self._channel_to_conns.get(name, set())
                listeners.discard(conn_id)
                if not listeners:
                    self._channel_to_conns.pop(name, None)
                    self._ctrl_q.put(("unsubscribe", name))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            restarting = self._thread is not None  # thread existed but died
            self._redis_client = redis.Redis(
                host=self._host,
                port=self._port,
                db=0,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            self._pubsub = self._redis_client.pubsub(ignore_subscribe_messages=True)
            self._thread = threading.Thread(target=self._run, name="ws-pool-reader", daemon=True)
            self._thread.start()
            if restarting:
                logger.warning(
                    "ws-pool reader thread RESTARTED (previous thread died) host=%s port=%s",
                    self._host, self._port,
                )
            else:
                logger.info("ws-pool reader thread started host=%s port=%s", self._host, self._port)

    def _dispatch(self, channel: str, data: Any) -> None:
        decoded: Any
        if isinstance(data, str):
            try:
                decoded = json.loads(data)
            except Exception:
                decoded = data
        else:
            decoded = data

        msg = {"type": "message", "channel": channel, "data": decoded}

        with self._lock:
            targets: List[Tuple["asyncio.Queue[Any]", asyncio.AbstractEventLoop]] = []
            for cid in self._channel_to_conns.get(channel, set()):
                conn = self._connections.get(cid)
                if conn:
                    targets.append((conn["queue"], conn["loop"]))
            for pat, cids in self._pattern_to_conns.items():
                try:
                    if fnmatch.fnmatchcase(channel, pat):
                        for cid in cids:
                            conn = self._connections.get(cid)
                            if conn:
                                targets.append((conn["queue"], conn["loop"]))
                except Exception:
                    pass

        for q, loop in targets:
            def _do_put(q: "asyncio.Queue[Any]" = q, m: Any = msg, ch: str = channel) -> None:
                if q.full():
                    try:
                        q.get_nowait()
                    except Exception:
                        pass
                    logger.debug("ws-pool queue full, oldest message dropped channel=%s", ch)
                try:
                    q.put_nowait(m)
                except Exception:
                    pass
            try:
                loop.call_soon_threadsafe(_do_put)
            except Exception:
                pass

    def _run(self) -> None:
        try:
            while True:
                while True:
                    try:
                        action, name = self._ctrl_q.get_nowait()
                    except Exception:
                        break
                    try:
                        if action == "subscribe":
                            self._pubsub.subscribe(name)
                            logger.info("ws-pool subscribed channel=%s", name)
                        elif action == "psubscribe":
                            self._pubsub.psubscribe(name)
                            logger.info("ws-pool psubscribed pattern=%s", name)
                        elif action == "unsubscribe":
                            self._pubsub.unsubscribe(name)
                            logger.info("ws-pool unsubscribed channel=%s", name)
                        elif action == "punsubscribe":
                            self._pubsub.punsubscribe(name)
                            logger.info("ws-pool punsubscribed pattern=%s", name)
                    except Exception as exc:
                        logger.warning("ws-pool %s failed name=%s: %s", action, name, exc)

                msg = self._pubsub.get_message(timeout=1.0)
                if not msg:
                    continue
                if msg.get("type") not in {"message", "pmessage"}:
                    continue

                channel = msg.get("channel") or ""
                if isinstance(channel, (bytes, bytearray)):
                    channel = channel.decode("utf-8", errors="replace")

                data = msg.get("data")
                if isinstance(data, (bytes, bytearray)):
                    try:
                        data = data.decode("utf-8")
                    except Exception:
                        data = str(data)

                self._dispatch(channel, data)

        except Exception as exc:
            logger.warning("ws-pool reader thread exited: %s", exc)
        finally:
            try:
                self._pubsub.close()
            except Exception:
                pass
            try:
                self._redis_client.close()
            except Exception:
                pass


_pool = SharedRedisPool()
