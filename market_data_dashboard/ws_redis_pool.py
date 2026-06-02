"""Shared Redis pub/sub reader pool for WebSocket connections.

Replaces the per-connection Thread model (C1 — arch/streams-loose-coupling).

Design:
  - At most MAX_POOL_THREADS background threads, each owning one Redis
    connection + one pubsub object.
  - Each WebSocket connection registers a ConnContext with an asyncio.Queue
    and a set of subscribed channels/patterns.
  - Pool threads dispatch received messages to every registered connection
    whose subscriptions match the incoming channel.
  - Queue is bounded (maxsize=100); when full, the oldest message is dropped
    (display-only — missing one tick is acceptable).
  - Connections are assigned to the pool thread with the fewest current
    subscribers (simple load-balancing).

Thread safety:
  - _registry (conn_id → ConnContext) is guarded by _registry_lock.
  - Each ConnContext's subscriptions set is guarded by its own lock.
  - Pool threads never mutate _registry; they only read it.

Lifecycle:
  - Pool threads start lazily on first register() call.
  - Pool threads are daemon threads; they exit when the process exits.
  - Unregister removes the connection; threads that have no subscriptions for
    a channel simply skip dispatch.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

import redis

logger = logging.getLogger(__name__)

MAX_POOL_THREADS = int(os.getenv("WS_REDIS_POOL_THREADS") or "4")
_QUEUE_MAXSIZE = 100


@dataclass
class ConnContext:
    conn_id: str
    async_loop: asyncio.AbstractEventLoop
    msg_queue: "asyncio.Queue[dict[str, Any]]"
    channels: Set[str] = field(default_factory=set)
    patterns: Set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def matches(self, channel: str) -> bool:
        with self.lock:
            if channel in self.channels:
                return True
            return any(fnmatch.fnmatchcase(channel, p) for p in self.patterns)

    def add_subscription(self, kind: str, name: str) -> None:
        with self.lock:
            if kind == "pattern":
                self.patterns.add(name)
            else:
                self.channels.add(name)

    def remove_subscription(self, kind: str, name: str) -> None:
        with self.lock:
            if kind == "pattern":
                self.patterns.discard(name)
            else:
                self.channels.discard(name)

    def all_channels(self) -> tuple[set[str], set[str]]:
        with self.lock:
            return set(self.channels), set(self.patterns)


class _PoolThread:
    """One background thread owning one Redis connection + pubsub."""

    def __init__(self, redis_host: str, redis_port: int) -> None:
        self._host = redis_host
        self._port = redis_port
        self._client: Optional[redis.Redis] = None
        self._pubsub: Any = None
        self._ctrl: "queue.SimpleQueue[tuple[str, str]]" = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-pool-redis")
        self._conn_ids: Set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._conn_ids)

    def add_conn(self, conn_id: str) -> None:
        with self._lock:
            self._conn_ids.add(conn_id)

    def remove_conn(self, conn_id: str) -> None:
        with self._lock:
            self._conn_ids.discard(conn_id)

    def send_ctrl(self, action: str, name: str) -> None:
        self._ctrl.put((action, name))

    def _run(self) -> None:
        self._client = redis.Redis(
            host=self._host,
            port=self._port,
            db=0,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        try:
            while True:
                while True:
                    try:
                        action, name = self._ctrl.get_nowait()
                    except Exception:
                        break
                    try:
                        if action == "subscribe":
                            self._pubsub.subscribe(name)
                        elif action == "psubscribe":
                            self._pubsub.psubscribe(name)
                        elif action == "unsubscribe":
                            self._pubsub.unsubscribe(name)
                        elif action == "punsubscribe":
                            self._pubsub.punsubscribe(name)
                    except Exception as exc:
                        logger.debug("pool ctrl error action=%s name=%s: %s", action, name, exc)

                msg = self._pubsub.get_message(timeout=1.0)
                if not msg:
                    continue

                msg_type = msg.get("type")
                if msg_type not in {"message", "pmessage"}:
                    continue

                channel = msg.get("channel") or ""
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8", errors="replace")

                data = msg.get("data")
                if isinstance(data, bytes):
                    try:
                        data = data.decode("utf-8")
                    except Exception:
                        data = str(data)

                _pool._dispatch(channel, data)

        except Exception as exc:
            logger.warning("ws-pool-redis thread exited: %s", exc)
        finally:
            try:
                self._pubsub.close()
            except Exception:
                pass
            try:
                self._client.close()
            except Exception:
                pass


class SharedRedisPool:
    """Module-level singleton pool. Use register()/unregister()/subscribe()."""

    def __init__(self) -> None:
        self._threads: list[_PoolThread] = []
        self._registry: Dict[str, ConnContext] = {}
        self._registry_lock = threading.Lock()
        self._started = False
        self._host = "localhost"
        self._port = 6379
        self._conn_to_thread: Dict[str, _PoolThread] = {}

    def configure(self, host: str, port: int) -> None:
        self._host = host
        self._port = port

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        for _ in range(MAX_POOL_THREADS):
            t = _PoolThread(self._host, self._port)
            t.start()
            self._threads.append(t)
        logger.info("ws-redis-pool started threads=%d", len(self._threads))

    def _least_loaded_thread(self) -> _PoolThread:
        return min(self._threads, key=lambda t: t.subscriber_count())

    def register(self, conn_id: str, loop: asyncio.AbstractEventLoop) -> ConnContext:
        self._ensure_started()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        ctx = ConnContext(conn_id=conn_id, async_loop=loop, msg_queue=q)
        with self._registry_lock:
            self._registry[conn_id] = ctx
            t = self._least_loaded_thread()
            t.add_conn(conn_id)
            self._conn_to_thread[conn_id] = t
        logger.debug("ws-pool register conn=%s", conn_id)
        return ctx

    def subscribe(self, conn_id: str, kind: str, name: str) -> None:
        with self._registry_lock:
            ctx = self._registry.get(conn_id)
            t = self._conn_to_thread.get(conn_id)
        if ctx is None or t is None:
            return
        ctx.add_subscription(kind, name)
        action = "psubscribe" if kind == "pattern" else "subscribe"
        t.send_ctrl(action, name)

    def unsubscribe(self, conn_id: str, kind: str, name: str) -> None:
        with self._registry_lock:
            ctx = self._registry.get(conn_id)
            t = self._conn_to_thread.get(conn_id)
        if ctx is None or t is None:
            return
        ctx.remove_subscription(kind, name)
        action = "punsubscribe" if kind == "pattern" else "unsubscribe"
        t.send_ctrl(action, name)

    def unregister(self, conn_id: str) -> None:
        with self._registry_lock:
            ctx = self._registry.pop(conn_id, None)
            t = self._conn_to_thread.pop(conn_id, None)
        if ctx is None:
            return
        channels, patterns = ctx.all_channels()
        if t is not None:
            for ch in channels:
                t.send_ctrl("unsubscribe", ch)
            for pat in patterns:
                t.send_ctrl("punsubscribe", pat)
            t.remove_conn(conn_id)
        logger.debug("ws-pool unregister conn=%s", conn_id)

    def _dispatch(self, channel: str, data: Any) -> None:
        with self._registry_lock:
            conns = list(self._registry.values())
        for ctx in conns:
            if not ctx.matches(channel):
                continue
            try:
                decoded: Any
                if isinstance(data, str):
                    try:
                        decoded = json.loads(data)
                    except Exception:
                        decoded = data
                else:
                    decoded = data
                msg = {
                    "type": "message",
                    "channel": channel,
                    "data": decoded,
                }
                try:
                    ctx.msg_queue.put_nowait(msg)
                except asyncio.QueueFull:
                    try:
                        ctx.msg_queue.get_nowait()
                    except Exception:
                        pass
                    try:
                        ctx.msg_queue.put_nowait(msg)
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug("dispatch error conn=%s: %s", ctx.conn_id, exc)


_pool = SharedRedisPool()
