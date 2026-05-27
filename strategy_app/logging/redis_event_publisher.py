from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

import redis

from contracts_app import isoformat_ist, redis_connection_kwargs


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
    def __init__(self, *, logger: logging.Logger) -> None:
        self._logger = logger
        self._enabled = str(os.getenv("STRATEGY_REDIS_PUBLISH_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}
        self._client: Optional[redis.Redis] = None
        if self._enabled:
            try:
                self._client = redis.Redis(**redis_connection_kwargs(decode_responses=True, for_pubsub=False))
            except Exception:
                self._logger.exception("failed to initialize strategy redis publisher")
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return bool(self._enabled and self._client is not None)

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            assert self._client is not None
            stream_prefix = "stream:"
            topic_text = str(topic or "").strip()
            payload = json.dumps(event, default=_json_default)
            if topic_text.startswith(stream_prefix):
                metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
                run_id = str(metadata.get("run_id") or "").strip()
                source_mode = str(metadata.get("source_mode") or "").strip()
                self._client.xadd(
                    topic_text,
                    {
                        "payload": payload,
                        "run_id": run_id,
                        "source_mode": source_mode,
                        "published_at": isoformat_ist(datetime.now()),
                    },
                )
                return
            self._client.publish(topic_text, payload)
        except Exception:
            self._logger.exception("failed to publish strategy event topic=%s; disabling redis publishing", topic)
            self._enabled = False
            try:
                if self._client is not None:
                    self._client.close()
            except Exception:
                pass
            self._client = None


__all__ = [
    "RedisEventPublisher",
]
