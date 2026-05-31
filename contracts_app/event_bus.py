from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from .config import redis_connection_kwargs
from .time_utils import isoformat_ist


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return isoformat_ist(value)
    if hasattr(value, "isoformat"):
        try:
            return isoformat_ist(value)
        except Exception:
            return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return str(value)


class EventBus(ABC):
    """Transport-agnostic event bus.

    All strategy_app decision stages must use this interface — never call
    redis.Redis directly in business logic.

    Routing convention (matches sim_namespace):
      - stream name starts with ``'stream:'``  → Redis Streams (XADD / XREADGROUP)
      - anything else                          → Redis PubSub (PUBLISH)
    """

    @abstractmethod
    def publish(self, stream: str, event: dict[str, Any]) -> None:
        """Publish an event dict to a stream or pub/sub topic."""

    @abstractmethod
    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 2000,
        stream_id: str = ">",
    ) -> list[tuple[str, dict[str, Any]]]:
        """Read messages from a Redis Stream consumer group.

        ``stream_id=">"``  — new messages only (default, normal operation).
        ``stream_id="0"``  — pending messages (use on startup to re-deliver
                              messages that were delivered but not acknowledged
                              in a prior run).

        Returns ``[(message_id, fields), ...]``.  Returns ``[]`` on timeout.
        The caller must call :meth:`acknowledge` after successful processing
        of each message so it is removed from the pending-entry list.
        """

    @abstractmethod
    def acknowledge(self, stream: str, group: str, message_id: str) -> None:
        """Acknowledge a message (XACK) — must be called after successful processing."""

    @abstractmethod
    def ensure_group(self, stream: str, group: str) -> None:
        """Create a consumer group if absent.  MKSTREAM-safe; swallows BUSYGROUP."""

    @abstractmethod
    def ping(self) -> bool:
        """Return ``True`` if the underlying transport is reachable."""


class RedisEventBus(EventBus):
    """Concrete Redis-backed EventBus.

    Uses ``redis_connection_kwargs()`` from contracts_app.config by default.
    Pass ``redis_kwargs`` to override for testing or alternate Redis instances.
    """

    _log = logging.getLogger(__name__)

    def __init__(self, *, redis_kwargs: Optional[dict[str, Any]] = None) -> None:
        import redis as _redis  # lazy so tests can patch before importing

        kwargs = dict(redis_kwargs or redis_connection_kwargs(decode_responses=True, for_pubsub=False))
        # socket_timeout must exceed the largest block_ms used in XREADGROUP calls
        # (currently 2000 ms = 2s).  Set 30s so blocking reads never time out.
        kwargs.setdefault("socket_timeout", 30.0)
        kwargs.setdefault("socket_connect_timeout", 5.0)
        self._client: _redis.Redis = _redis.Redis(**kwargs)

    # ── publish ────────────────────────────────────────────────────────────

    def publish(self, stream: str, event: dict[str, Any]) -> None:
        payload = json.dumps(event, default=_json_default)
        if stream.startswith("stream:"):
            meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            self._client.xadd(
                stream,
                {
                    "payload": payload,
                    "run_id": str(meta.get("run_id") or event.get("run_id") or ""),
                    "source_mode": str(meta.get("source_mode") or event.get("parity_mode") or ""),
                    "published_at": isoformat_ist(),
                },
            )
        else:
            self._client.publish(stream, payload)

    # ── consume ────────────────────────────────────────────────────────────

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 2000,
        stream_id: str = ">",
    ) -> list[tuple[str, dict[str, Any]]]:
        results = self._client.xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={stream: stream_id},
            count=count,
            block=block_ms,
        )
        if not results:
            return []
        _, messages = results[0]
        return list(messages)  # [(msg_id, fields), ...]

    # ── acknowledge ────────────────────────────────────────────────────────

    def acknowledge(self, stream: str, group: str, message_id: str) -> None:
        self._client.xack(stream, group, message_id)

    # ── group management ───────────────────────────────────────────────────

    def ensure_group(self, stream: str, group: str) -> None:
        try:
            self._client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                return
            raise

    # ── health ─────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False


__all__ = ["EventBus", "RedisEventBus"]
