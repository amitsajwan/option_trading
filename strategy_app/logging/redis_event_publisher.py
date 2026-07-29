from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

from contracts_app import isoformat_ist
from contracts_app.event_bus import EventBus, RedisEventBus


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


class RedisEventPublisher:
    """Thin publish-only facade over :class:`EventBus`.

    Accepts an injected ``bus`` so callers can swap the transport in tests.
    When no ``bus`` is provided a :class:`RedisEventBus` is created using
    the default ``redis_connection_kwargs``, and a fresh one is created again
    to recover from a failed publish (rate-limited by
    ``_RECONNECT_COOLDOWN_SEC`` so a genuinely-down Redis doesn't get
    hammered / log-flooded).

    Publishing is guarded by the ``STRATEGY_REDIS_PUBLISH_ENABLED`` env var
    (default: enabled).

    BUG (found 2026-07-29): this used to disable publishing for the lifetime
    of the instance after a single failure, with no reconnect path. Live
    BankNifty strategy_app hit one transient publish failure and then went
    dark to persistence_app for 5+ days straight -- JSONL (canonical) kept
    logging fine the whole time, so trading itself was unaffected, but the
    Mongo/dashboard sync silently stopped with no further error logged (that
    silence was itself by design, to avoid log-flooding) and nothing ever
    brought it back because there was no retry. Now it self-heals.
    """

    _RECONNECT_COOLDOWN_SEC = 30.0

    def __init__(self, *, bus: Optional[EventBus] = None, logger: logging.Logger) -> None:
        self._logger = logger
        self._enabled = (
            str(os.getenv("STRATEGY_REDIS_PUBLISH_ENABLED") or "1").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        # None means "own a real RedisEventBus, reconnect on failure";
        # non-None (test injection) is reused as-is, never recreated.
        self._injected_bus = bus
        self._bus: Optional[EventBus] = None
        self._last_reconnect_attempt: float = 0.0
        if self._enabled:
            self._bus = self._new_bus()
            if self._bus is None:
                self._enabled = False

    def _new_bus(self) -> Optional[EventBus]:
        if self._injected_bus is not None:
            return self._injected_bus
        try:
            return RedisEventBus()
        except Exception:
            self._logger.exception("failed to initialize strategy redis publisher")
            return None

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        if not self._enabled:
            return
        if self._bus is None:
            now = time.monotonic()
            if now - self._last_reconnect_attempt < self._RECONNECT_COOLDOWN_SEC:
                return  # rate-limited: stay quiet between reconnect attempts
            self._last_reconnect_attempt = now
            self._bus = self._new_bus()
            if self._bus is None:
                return
            self._logger.info("strategy redis publisher reconnected topic=%s", topic)
        try:
            self._bus.publish(str(topic or "").strip(), event)
        except Exception:
            self._logger.exception(
                "failed to publish strategy event topic=%s; will retry after %.0fs",
                topic, self._RECONNECT_COOLDOWN_SEC,
            )
            self._bus = None
            self._last_reconnect_attempt = time.monotonic()


__all__ = [
    "RedisEventPublisher",
    "_json_default",
]
