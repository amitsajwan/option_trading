from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from contracts_app import TimestampSourceMode, ensure_ist, isoformat_ist, parse_timestamp_to_ist

IST = ensure_ist(datetime(2000, 1, 1)).tzinfo


def parse_market_timestamp_ist(value: Any, *, naive_mode: TimestampSourceMode = TimestampSourceMode.MARKET_IST) -> Optional[datetime]:
    """Parse market timestamps with IST-first semantics.

    Rules:
    - Offset-aware inputs keep their original offset and are converted to IST.
    - Naive inputs are treated as IST.
    """
    return parse_timestamp_to_ist(value, naive_mode=naive_mode)


def to_ist(ts: datetime) -> datetime:
    return ensure_ist(ts)


def to_ist_iso(ts: datetime, *, naive_mode: TimestampSourceMode = TimestampSourceMode.MARKET_IST) -> str:
    return isoformat_ist(ts, naive_mode=naive_mode)


def minute_bucket_ist(ts: datetime) -> datetime:
    return ensure_ist(ts).replace(second=0, microsecond=0)
