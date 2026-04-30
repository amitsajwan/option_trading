from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

from .time_utils import TimestampSourceMode, isoformat_ist, parse_timestamp_to_ist


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


def _normalize_published_at(value: Optional[Any]) -> str:
    parsed = parse_timestamp_to_ist(value, naive_mode=TimestampSourceMode.MARKET_IST) if value is not None else None
    return isoformat_ist(parsed)


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
        published_at=_normalize_published_at(published_at),
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


def _build_event(
    *,
    event_type: str,
    event_version: str,
    source: str,
    body_key: str,
    body: dict[str, Any],
    published_at: Optional[str] = None,
    event_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(body or {})
    return {
        "event_type": str(event_type),
        "event_version": str(event_version),
        "event_id": str(event_id or uuid4()),
        "source": str(source or "strategy_app"),
        "published_at": _normalize_published_at(published_at),
        body_key: payload,
        "metadata": dict(metadata or {}),
    }


def _parse_event(payload: dict[str, Any], *, event_type: str, body_key: str) -> Optional[dict[str, Any]]:
    event = dict(payload or {})
    if str(event.get("event_type") or "") != event_type:
        return None
    if str(event.get("event_version") or "") != "1.0":
        return None
    if not isinstance(event.get(body_key), dict):
        return None
    return event


def build_strategy_vote_event(
    *,
    vote: dict[str, Any],
    source: str,
    event_id: Optional[str] = None,
    published_at: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return _build_event(
        event_type="strategy_vote",
        event_version="1.0",
        source=source,
        body_key="vote",
        body=vote,
        event_id=event_id,
        published_at=published_at,
        metadata=metadata,
    )


def parse_strategy_vote_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _parse_event(payload, event_type="strategy_vote", body_key="vote")


def build_trade_signal_event(
    *,
    signal: dict[str, Any],
    source: str,
    event_id: Optional[str] = None,
    published_at: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return _build_event(
        event_type="trade_signal",
        event_version="1.0",
        source=source,
        body_key="signal",
        body=signal,
        event_id=event_id,
        published_at=published_at,
        metadata=metadata,
    )


def parse_trade_signal_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _parse_event(payload, event_type="trade_signal", body_key="signal")


def build_strategy_position_event(
    *,
    position: dict[str, Any],
    source: str,
    event_id: Optional[str] = None,
    published_at: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return _build_event(
        event_type="strategy_position",
        event_version="1.0",
        source=source,
        body_key="position",
        body=position,
        event_id=event_id,
        published_at=published_at,
        metadata=metadata,
    )


def parse_strategy_position_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _parse_event(payload, event_type="strategy_position", body_key="position")


def build_strategy_decision_trace_event(
    *,
    trace: dict[str, Any],
    source: str,
    event_id: Optional[str] = None,
    published_at: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return _build_event(
        event_type="strategy_decision_trace",
        event_version="1.0",
        source=source,
        body_key="trace",
        body=trace,
        event_id=event_id,
        published_at=published_at,
        metadata=metadata,
    )


def parse_strategy_decision_trace_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _parse_event(payload, event_type="strategy_decision_trace", body_key="trace")
