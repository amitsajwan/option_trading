from __future__ import annotations

import logging
import os
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
    the default ``redis_connection_kwargs``.

    Publishing is guarded by the ``STRATEGY_REDIS_PUBLISH_ENABLED`` env var
    (default: enabled).  A single publish failure disables further publishing
    for the lifetime of this instance to avoid log-flooding on Redis outages.
    """

    def __init__(self, *, bus: Optional[EventBus] = None, logger: logging.Logger) -> None:
        self._logger = logger
        self._enabled = (
            str(os.getenv("STRATEGY_REDIS_PUBLISH_ENABLED") or "1").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        self._bus: Optional[EventBus] = None
        if self._enabled:
            try:
                self._bus = bus if bus is not None else RedisEventBus()
            except Exception:
                self._logger.exception("failed to initialize strategy redis publisher")
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return bool(self._enabled and self._bus is not None)

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            assert self._bus is not None
            self._bus.publish(str(topic or "").strip(), event)
        except Exception:
            self._logger.exception(
                "failed to publish strategy event topic=%s; disabling redis publishing", topic
            )
            self._enabled = False
            self._bus = None


__all__ = [
    "RedisEventPublisher",
    "_json_default",
]
