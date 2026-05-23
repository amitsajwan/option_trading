"""Runtime adapters for strategy_app."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .redis_snapshot_consumer import RedisSnapshotConsumer

__all__ = ["RedisSnapshotConsumer"]


def __getattr__(name: str):
    if name == "RedisSnapshotConsumer":
        from .redis_snapshot_consumer import RedisSnapshotConsumer

        return RedisSnapshotConsumer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
