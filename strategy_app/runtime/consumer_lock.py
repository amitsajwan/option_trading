"""Redis single-consumer lock for snapshot pub/sub topics.

Owner string formats:
  - v2 (preferred): ``{instance_id}|{host}:{pid}:{token}:{topic}``
  - v1 (legacy):    ``{host}:{pid}:{token}:{topic}``  (instance_id inferred from host)

``STRATEGY_CONSUMER_LOCK_INSTANCE_ID`` is a stable logical identity (e.g.
``strategy_app_historical``). It survives Docker force-recreate, which changes
container hostname but must not block the replacement consumer for one TTL.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from threading import Event
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

OWNER_SEP = "|"
OWNER_FIELDS = 4


class _RedisLockClient(Protocol):
    def set(self, name: str, value: str, *, nx: bool = ..., ex: int = ...) -> Any: ...
    def get(self, name: str) -> Any: ...
    def eval(self, script: str, numkeys: int, *args: Any) -> Any: ...
    def delete(self, *names: str) -> Any: ...
    def expire(self, name: str, time: int) -> Any: ...


@dataclass(frozen=True)
class ConsumerLockOwner:
    instance_id: str
    host: str
    pid: str
    token: str
    topic: str

    def serialize(self) -> str:
        """Redis lock value; v2 when instance_id differs from host."""
        tail = f"{self.host}:{self.pid}:{self.token}:{self.topic}"
        if self.instance_id and self.instance_id != self.host:
            return f"{self.instance_id}{OWNER_SEP}{tail}"
        return tail

    @classmethod
    def parse(cls, raw: str) -> ConsumerLockOwner:
        text = str(raw or "").strip()
        if not text:
            return cls(instance_id="", host="", pid="", token="", topic="")
        if OWNER_SEP in text:
            instance_id, tail = text.split(OWNER_SEP, 1)
        else:
            instance_id, tail = "", text
        parts = tail.split(":")
        if len(parts) < OWNER_FIELDS:
            host = parts[0] if parts else ""
            return cls(
                instance_id=instance_id or host,
                host=host,
                pid=parts[1] if len(parts) > 1 else "",
                token=parts[2] if len(parts) > 2 else "",
                topic=":".join(parts[3:]) if len(parts) > 3 else "",
            )
        host, pid, token = parts[0], parts[1], parts[2]
        topic = ":".join(parts[3:])
        return cls(
            instance_id=instance_id or host,
            host=host,
            pid=pid,
            token=token,
            topic=topic,
        )


def lock_key_for_topic(topic: str) -> str:
    return f"strategy_app:consumer_lock:{topic}"


def resolve_lock_instance_id() -> str:
    for env_name in ("STRATEGY_CONSUMER_LOCK_INSTANCE_ID", "STRATEGY_CONSUMER_SERVICE_NAME"):
        value = str(os.getenv(env_name) or "").strip()
        if value:
            return value
    return socket.gethostname()


def build_lock_owner(topic: str) -> ConsumerLockOwner:
    return ConsumerLockOwner(
        instance_id=resolve_lock_instance_id(),
        host=socket.gethostname(),
        pid=str(os.getpid()),
        token=uuid.uuid4().hex[:8],
        topic=topic,
    )


def owners_are_reclaimable(existing: ConsumerLockOwner, ours: ConsumerLockOwner) -> bool:
    if existing.instance_id and ours.instance_id and existing.instance_id == ours.instance_id:
        return True
    return bool(existing.host) and existing.host == ours.host


def lock_config_from_env() -> tuple[bool, int, int, int]:
    lock_enabled_raw = str(
        os.getenv("STRATEGY_CONSUMER_LOCK_ENABLED")
        or os.getenv("STRATEGY_SINGLE_CONSUMER_LOCK_ENABLED")
        or "1"
    ).strip().lower()
    enabled = lock_enabled_raw not in {"0", "false", "no", "off"}
    try:
        ttl_sec = int(str(os.getenv("STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC") or "120").strip())
    except Exception:
        ttl_sec = 120
    ttl_sec = max(5, ttl_sec)
    try:
        refresh_sec = int(str(os.getenv("STRATEGY_SINGLE_CONSUMER_LOCK_REFRESH_SEC") or "30").strip())
    except Exception:
        refresh_sec = 30
    refresh_sec = max(5, min(refresh_sec, ttl_sec // 2))
    try:
        max_wait_raw = str(os.getenv("STRATEGY_SINGLE_CONSUMER_LOCK_MAX_WAIT_SEC") or "").strip()
        max_wait_sec = int(max_wait_raw) if max_wait_raw else ttl_sec + 5
    except Exception:
        max_wait_sec = ttl_sec + 5
    max_wait_sec = max(ttl_sec + 5, max_wait_sec)
    return enabled, ttl_sec, refresh_sec, max_wait_sec


class ConsumerLock:
    """Acquire / refresh / release a per-topic Redis consumer lock."""

    def __init__(
        self,
        client: _RedisLockClient,
        *,
        topic: str,
        stop_event: Optional[Event] = None,
        enabled: Optional[bool] = None,
        ttl_sec: Optional[int] = None,
        refresh_sec: Optional[int] = None,
        max_wait_sec: Optional[int] = None,
        instance_id: Optional[str] = None,
    ) -> None:
        env_enabled, env_ttl, env_refresh, env_max_wait = lock_config_from_env()
        self._client = client
        self._topic = topic
        self._key = lock_key_for_topic(topic)
        self._stop_event = stop_event or Event()
        self._enabled = env_enabled if enabled is None else enabled
        self._ttl_sec = env_ttl if ttl_sec is None else max(5, ttl_sec)
        self._refresh_sec = env_refresh if refresh_sec is None else max(5, min(refresh_sec, self._ttl_sec // 2))
        self._max_wait_sec = env_max_wait if max_wait_sec is None else max(self._ttl_sec + 5, max_wait_sec)
        if instance_id is not None:
            self._owner = ConsumerLockOwner(
                instance_id=instance_id,
                host=socket.gethostname(),
                pid=str(os.getpid()),
                token=uuid.uuid4().hex[:8],
                topic=topic,
            )
        else:
            self._owner = build_lock_owner(topic)
        self._owner_str = self._owner.serialize()
        self._lock_refresh_failed = False

    @property
    def key(self) -> str:
        return self._key

    @property
    def owner(self) -> str:
        return self._owner_str

    @property
    def refresh_interval_sec(self) -> int:
        return self._refresh_sec

    @property
    def lock_refresh_failed(self) -> bool:
        return self._lock_refresh_failed

    def acquire(self) -> None:
        if not self._enabled:
            return
        if not hasattr(self._client, "set"):
            logger.warning("strategy consumer lock skipped: redis client does not support SET")
            return

        max_wait_sec = self._max_wait_sec
        wait_step_sec = max(2, min(10, self._ttl_sec // 12))
        deadline = time.monotonic() + max_wait_sec
        attempt = 0
        ours = self._owner

        while True:
            attempt += 1
            try:
                acquired = self._client.set(
                    self._key,
                    self._owner_str,
                    nx=True,
                    ex=self._ttl_sec,
                )
            except Exception:
                logger.exception("strategy consumer lock acquire failed key=%s", self._key)
                raise
            if acquired:
                logger.info(
                    "strategy consumer lock acquired key=%s ttl=%ss owner=%s attempt=%d",
                    self._key,
                    self._ttl_sec,
                    self._owner_str,
                    attempt,
                )
                return

            existing_owner_raw = None
            if hasattr(self._client, "get"):
                try:
                    existing_owner_raw = self._client.get(self._key)
                except Exception:
                    existing_owner_raw = None
            existing_owner_text = (
                existing_owner_raw.decode("utf-8", errors="replace")
                if isinstance(existing_owner_raw, (bytes, bytearray))
                else existing_owner_raw
            )
            existing = ConsumerLockOwner.parse(str(existing_owner_text or ""))

            if owners_are_reclaimable(existing, ours):
                stolen = self._try_reclaim(existing_owner_text)
                if stolen:
                    logger.info(
                        "strategy consumer lock reclaimed key=%s prior_owner=%s new_owner=%s",
                        self._key,
                        existing_owner_text,
                        self._owner_str,
                    )
                    return
                logger.info(
                    "stale-lock reclaim attempt did not commit, will retry (existing_owner=%s)",
                    existing_owner_text,
                )

            now = time.monotonic()
            if now >= deadline:
                raise RuntimeError(
                    "duplicate strategy consumer detected after waiting "
                    f"{max_wait_sec}s for topic={self._topic} lock_key={self._key} "
                    f"existing_owner={existing_owner_text!r} (attempts={attempt})"
                )

            remaining = max(0, int(deadline - now))
            same_kind = "same" if owners_are_reclaimable(existing, ours) else "different"
            logger.warning(
                "strategy consumer lock contended (%s); existing_owner=%s — "
                "retry in %ds (attempt %d, %ds before timeout)",
                same_kind,
                existing_owner_text,
                wait_step_sec,
                attempt,
                remaining,
            )
            if self._stop_event.wait(wait_step_sec):
                logger.info("stop requested while waiting on consumer lock; aborting acquire")
                return

    def _try_reclaim(self, existing_owner_text: Optional[str]) -> bool:
        if not existing_owner_text:
            return False
        if hasattr(self._client, "eval"):
            try:
                result = self._client.eval(
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                    "return redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]) "
                    "else return nil end",
                    1,
                    self._key,
                    existing_owner_text,
                    self._owner_str,
                    self._ttl_sec,
                )
                return bool(result)
            except Exception:
                logger.exception(
                    "stale lock reclaim (CAS) failed key=%s; will fall back to wait loop",
                    self._key,
                )
                return False
        if hasattr(self._client, "delete"):
            try:
                self._client.delete(self._key)
            except Exception:
                pass
        return False

    def refresh(self) -> None:
        if not self._enabled:
            return
        if not all(hasattr(self._client, method) for method in ("get", "expire")):
            return
        key = self._key
        owner = self._owner_str
        ttl = int(self._ttl_sec)
        try:
            if hasattr(self._client, "eval"):
                refreshed = self._client.eval(
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                    "return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end",
                    1,
                    key,
                    owner,
                    ttl,
                )
                if not int(refreshed or 0):
                    self._lock_refresh_failed = True
                    raise RuntimeError(
                        f"strategy consumer lock lost for topic={self._topic} key={key}"
                    )
            else:
                current_owner = self._client.get(key)
                if current_owner == owner:
                    self._client.expire(key, ttl)
                else:
                    self._lock_refresh_failed = True
                    raise RuntimeError(
                        f"strategy consumer lock lost for topic={self._topic} key={key}"
                    )
        except RuntimeError:
            raise
        except Exception:
            logger.exception("strategy consumer lock refresh failed key=%s", key)
            raise

    def release(self) -> None:
        if not self._enabled:
            return
        if not hasattr(self._client, "get"):
            return
        key = self._key
        owner = self._owner_str
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


def holder_host_for_ops(owner_raw: str) -> str:
    """Docker container id / hostname segment used for ``docker inspect``."""
    return ConsumerLockOwner.parse(owner_raw).host
