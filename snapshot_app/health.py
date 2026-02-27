from __future__ import annotations

import argparse
import json
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from contracts_app import (
    find_matching_python_processes,
    is_market_open_ist,
    is_trading_day_ist,
    load_holidays,
)

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_MARKET_TZ = "Asia/Kolkata"
DEFAULT_MARKET_OPEN = "09:15"
DEFAULT_MARKET_CLOSE = "15:30"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _zone_or_ist(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_MARKET_TZ)


def _ist_now_iso() -> str:
    return datetime.now(tz=IST).isoformat()


def _parse_iso_ist(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _read_last_line(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        tail = deque(handle, maxlen=1)
    if not tail:
        return None
    return str(tail[0]).strip()


def evaluate(*, events_path: str, max_age_seconds: float) -> tuple[dict[str, Any], int]:
    path = Path(events_path).resolve()
    process_matches = find_matching_python_processes(["snapshot_app.main_live"])
    process_running = len(process_matches) > 0
    market_session_enabled = _truthy(os.getenv("MARKET_SESSION_ENABLED", "0"))
    market_timezone = str(os.getenv("MARKET_TIMEZONE") or DEFAULT_MARKET_TZ).strip() or DEFAULT_MARKET_TZ
    market_open_time = str(os.getenv("MARKET_OPEN_TIME") or DEFAULT_MARKET_OPEN).strip() or DEFAULT_MARKET_OPEN
    market_close_time = str(os.getenv("MARKET_CLOSE_TIME") or DEFAULT_MARKET_CLOSE).strip() or DEFAULT_MARKET_CLOSE
    holidays_file = str(os.getenv("NSE_HOLIDAYS_FILE") or "").strip()

    market_open = None
    trading_day = None
    if market_session_enabled:
        now_ist = datetime.now(tz=_zone_or_ist(market_timezone))
        holidays = load_holidays(holidays_file)
        market_open = is_market_open_ist(now_ist, market_open_time, market_close_time, holidays)
        trading_day = is_trading_day_ist(now_ist, holidays)

    last_line = _read_last_line(path)
    last_event: Optional[dict[str, Any]] = None
    parse_error: Optional[str] = None
    if last_line:
        try:
            obj = json.loads(last_line)
            if isinstance(obj, dict):
                last_event = obj
        except Exception as exc:
            parse_error = str(exc)

    published_at = None
    snapshot_id = None
    age_seconds = None
    if last_event is not None:
        published_at = str(last_event.get("published_at") or "")
        snapshot_id = str(last_event.get("snapshot_id") or "")
        dt = _parse_iso_ist(published_at)
        if dt is not None:
            age_seconds = (datetime.now(tz=IST) - dt).total_seconds()

    status = "healthy"
    code = 0
    if market_session_enabled and market_open is False:
        status = "healthy" if process_running else "degraded"
        code = 0 if process_running else 1
    elif last_event is None:
        status = "unhealthy"
        code = 2
    elif age_seconds is not None and age_seconds > float(max_age_seconds):
        status = "degraded" if process_running else "unhealthy"
        code = 1 if process_running else 2
    elif not process_running:
        status = "degraded"
        code = 1

    result = {
        "component": "snapshot_app",
        "checked_at_ist": _ist_now_iso(),
        "status": status,
        "process": {
            "running": process_running,
            "count": len(process_matches),
            "pids": [int(pid) for pid, _ in process_matches[:10]],
        },
        "events": {
            "path": str(path),
            "exists": path.exists(),
            "last_snapshot_id": snapshot_id or None,
            "last_published_at_ist": published_at or None,
            "last_event_age_seconds": round(float(age_seconds), 3) if age_seconds is not None else None,
            "parse_error": parse_error,
        },
        "session_gate": {
            "enabled": market_session_enabled,
            "market_open": market_open,
            "trading_day": trading_day,
            "timezone": market_timezone if market_session_enabled else None,
            "open_time": market_open_time if market_session_enabled else None,
            "close_time": market_close_time if market_session_enabled else None,
            "holidays_file": holidays_file or None,
        },
    }
    return result, code


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Health check for snapshot_app")
    parser.add_argument("--events-path", default=".run/snapshot_app/events.jsonl")
    parser.add_argument("--max-age-seconds", type=float, default=180.0)
    args = parser.parse_args(list(argv) if argv is not None else None)

    result, code = evaluate(events_path=str(args.events_path), max_age_seconds=float(args.max_age_seconds))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(run_cli())
