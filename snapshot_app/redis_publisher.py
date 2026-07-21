from __future__ import annotations

import json
import logging
from typing import Any, Optional

import redis

from contracts_app import redis_connection_kwargs
from .publisher import EventPublisher

logger = logging.getLogger(__name__)
_STREAM_MAXLEN = 500


def _redis_client() -> redis.Redis:
    return redis.Redis(**redis_connection_kwargs(decode_responses=True))


def _stream_name_for_topic(topic: str) -> str:
    if "historical" in topic:
        return "stream:snapshots:historical"
    return "stream:snapshots:live"


class RedisEventPublisher(EventPublisher):
    def __init__(self, client: Optional[redis.Redis] = None) -> None:
        self._client = client or _redis_client()
        self._published = 0
        self._errors = 0

    def publish(self, *, topic: str, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload or {}, ensure_ascii=False, default=str)
        stream_name = _stream_name_for_topic(str(topic))
        try:
            self._client.xadd(
                stream_name,
                {"payload": serialized, "topic": str(topic)},
                maxlen=_STREAM_MAXLEN,
                approximate=True,
            )
            self._published += 1
            if self._published == 1 or self._published % 500 == 0:
                logger.info(
                    "snapshot_publisher xadd stream=%s published=%s errors=%s",
                    stream_name, self._published, self._errors,
                )
        except Exception as exc:
            self._errors += 1
            logger.warning(
                "snapshot_publisher xadd failed stream=%s topic=%s errors=%s: %s",
                stream_name, topic, self._errors, exc,
            )
