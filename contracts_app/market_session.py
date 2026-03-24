from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Set
from zoneinfo import ZoneInfo


def _resolve_ist_zone():
    try:
        return ZoneInfo("Asia/Kolkata")
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


IST_ZONE = _resolve_ist_zone()


def _coerce_ist(now_ist: datetime) -> datetime:
    if not isinstance(now_ist, datetime):
        raise TypeError("now_ist must be datetime")
    if now_ist.tzinfo is None:
        return now_ist.replace(tzinfo=IST_ZONE)
    return now_ist.astimezone(IST_ZONE)


def _parse_hhmm(raw: str | time) -> time:
    if isinstance(raw, time):
        return raw
    text = str(raw or "").strip()
    if len(text) != 5 or text[2] != ":":
        raise ValueError(f"invalid HH:MM time: {raw}")
    hh = int(text[:2])
    mm = int(text[3:])
    return time(hour=hh, minute=mm)


def _normalize_holidays(holidays: Optional[Iterable[object]]) -> Set[date]:
    out: Set[date] = set()
    if not holidays:
        return out
    for raw in holidays:
        if isinstance(raw, date):
            out.add(raw)
            continue
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            out.add(date.fromisoformat(text))
        except Exception:
            continue
    return out


def load_holidays(path: str | Path | None) -> Set[date]:
    if not path:
        return set()
    file_path = Path(path)
    if not file_path.exists():
        return set()
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return set()

    if isinstance(payload, list):
        return _normalize_holidays(payload)
    if isinstance(payload, dict):
        if isinstance(payload.get("holidays"), list):
            return _normalize_holidays(payload.get("holidays"))
        if isinstance(payload.get("dates"), list):
            return _normalize_holidays(payload.get("dates"))
    return set()


def is_trading_day_ist(now_ist: datetime, holidays: Optional[Iterable[object]]) -> bool:
    now = _coerce_ist(now_ist)
    day = now.date()
    if now.weekday() >= 5:
        return False
    return day not in _normalize_holidays(holidays)


def is_market_open_ist(
    now_ist: datetime,
    open_time: str | time,
    close_time: str | time,
    holidays: Optional[Iterable[object]],
) -> bool:
    now = _coerce_ist(now_ist)
    if not is_trading_day_ist(now, holidays):
        return False
    open_t = _parse_hhmm(open_time)
    close_t = _parse_hhmm(close_time)
    now_t = now.timetz().replace(tzinfo=None)
    return open_t <= now_t <= close_t


def seconds_until_next_open_ist(
    now_ist: datetime,
    open_time: str | time,
    holidays: Optional[Iterable[object]],
) -> int:
    now = _coerce_ist(now_ist)
    open_t = _parse_hhmm(open_time)
    holiday_set = _normalize_holidays(holidays)

    candidate_day = now.date()
    while True:
        if candidate_day.weekday() < 5 and candidate_day not in holiday_set:
            open_dt = datetime.combine(candidate_day, open_t, tzinfo=IST_ZONE)
            if open_dt > now:
                delta = open_dt - now
                return max(1, int(delta.total_seconds()))
        candidate_day = candidate_day + timedelta(days=1)
