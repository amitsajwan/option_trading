from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now_iso() -> str:
    return datetime.now(tz=IST).isoformat()


@dataclass
class SnapshotEventEnvelope:
    event_type: str
    event_version: str
    event_id: str
    source: str
    published_at: str
    snapshot_id: str
    snapshot: dict[str, Any]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "event_version": self.event_version,
            "event_id": self.event_id,
            "source": self.source,
            "published_at": self.published_at,
            "snapshot_id": self.snapshot_id,
            "snapshot": self.snapshot,
            "metadata": self.metadata,
        }


def build_snapshot_event(
    *,
    snapshot: dict[str, Any],
    source: str,
    event_id: Optional[str] = None,
    published_at: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    snap = dict(snapshot or {})
    envelope = SnapshotEventEnvelope(
        event_type="market_snapshot",
        event_version="1.0",
        event_id=str(event_id or uuid4()),
        source=str(source or "snapshot_app"),
        published_at=str(published_at or _ist_now_iso()),
        snapshot_id=str(snap.get("snapshot_id") or ""),
        snapshot=snap,
        metadata=dict(metadata or {}),
    )
    return envelope.to_dict()


def parse_snapshot_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    event = dict(payload or {})
    if str(event.get("event_type") or "") != "market_snapshot":
        return None
    if str(event.get("event_version") or "") != "1.0":
        return None
    if not isinstance(event.get("snapshot"), dict):
        return None
    if not str(event.get("snapshot_id") or ""):
        return None
    return event
