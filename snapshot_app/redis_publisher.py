from __future__ import annotations

import json
from typing import Any, Optional

import redis

from contracts_app import redis_connection_kwargs
from .publisher import EventPublisher


def _redis_client() -> redis.Redis:
    return redis.Redis(**redis_connection_kwargs(decode_responses=True))


class RedisEventPublisher(EventPublisher):
    def __init__(self, client: Optional[redis.Redis] = None) -> None:
        self._client = client or _redis_client()

    def publish(self, *, topic: str, payload: dict[str, Any]) -> None:
        self._client.publish(str(topic), json.dumps(payload or {}, ensure_ascii=False, default=str))
