from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo


def _resolve_ist_zone():
    try:
        return ZoneInfo("Asia/Kolkata")
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


IST_ZONE = _resolve_ist_zone()
UTC_ZONE = timezone.utc


class TimestampSourceMode(str, Enum):
    MARKET_IST = "market_ist"
    LEGACY_MONGO_UTC = "legacy_mongo_utc"
    GENERIC = "generic"


def now_ist() -> datetime:
    return datetime.now(tz=IST_ZONE)


def ensure_ist(value: datetime, *, naive_mode: TimestampSourceMode = TimestampSourceMode.MARKET_IST) -> datetime:
    dt = value
    if dt.tzinfo is None:
        if naive_mode == TimestampSourceMode.LEGACY_MONGO_UTC:
            dt = dt.replace(tzinfo=UTC_ZONE)
        else:
            dt = dt.replace(tzinfo=IST_ZONE)
    return dt.astimezone(IST_ZONE)


def isoformat_ist(value: Optional[datetime] = None, *, naive_mode: TimestampSourceMode = TimestampSourceMode.MARKET_IST) -> str:
    dt = ensure_ist(value or now_ist(), naive_mode=naive_mode)
    return dt.isoformat()


def parse_timestamp_to_ist(
    value: Any,
    *,
    naive_mode: TimestampSourceMode = TimestampSourceMode.MARKET_IST,
) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_ist(value, naive_mode=naive_mode)

    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    return ensure_ist(parsed, naive_mode=naive_mode)


def format_log_time_ist(record_created_epoch: float, datefmt: Optional[str] = None) -> str:
    dt = datetime.fromtimestamp(record_created_epoch, tz=IST_ZONE)
    if datefmt:
        return dt.strftime(datefmt)
    return dt.isoformat(sep=" ", timespec="milliseconds")
