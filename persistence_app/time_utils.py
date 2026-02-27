from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional


IST = timezone(timedelta(hours=5, minutes=30))


def parse_market_timestamp_ist(value: Any) -> Optional[datetime]:
    """Parse market timestamps with IST-first semantics.

    Rules:
    - Offset-aware inputs keep their original offset and are converted to IST.
    - Naive inputs are treated as IST.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def to_ist(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def minute_bucket_ist(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return ts.astimezone(IST).replace(second=0, microsecond=0)
