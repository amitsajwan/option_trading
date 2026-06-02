from __future__ import annotations

import json
import os
from typing import Any, Optional

import redis

from contracts_app import redis_connection_kwargs
from .publisher import EventPublisher

_STREAM_MAXLEN = 500


def _redis_client() -> redis.Redis:
    return redis.Redis(**redis_connection_kwargs(decode_responses=True))


def _pubsub_shadow_enabled() -> bool:
    return str(os.getenv("SNAPSHOT_PUBSUB_SHADOW") or "true").strip().lower() not in {"0", "false", "no", "off"}


def _stream_name_for_topic(topic: str) -> str:
    if "historical" in topic:
        return "stream:snapshots:historical"
    return "stream:snapshots:live"


class RedisEventPublisher(EventPublisher):
    def __init__(self, client: Optional[redis.Redis] = None) -> None:
        self._client = client or _redis_client()

    def publish(self, *, topic: str, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload or {}, ensure_ascii=False, default=str)
        stream_name = _stream_name_for_topic(str(topic))
        self._client.xadd(
            stream_name,
            {"payload": serialized, "topic": str(topic)},
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
        if _pubsub_shadow_enabled():
            self._client.publish(str(topic), serialized)
