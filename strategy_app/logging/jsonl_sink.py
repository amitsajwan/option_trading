from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from contracts_app import isoformat_ist


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return isoformat_ist(value)
    if hasattr(value, "isoformat"):
        try:
            return isoformat_ist(value)
        except Exception:
            return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return str(value)


def normalize_record_timestamps(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    for key, value in list(out.items()):
        if key in {"timestamp", "entry_time", "exit_time", "published_at", "received_at_ist"} and isinstance(value, datetime):
            out[key] = isoformat_ist(value)
    return out


def append_jsonl(path: Path, record: dict[str, Any], *, logger: logging.Logger) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=_json_default) + "\n")
    except Exception:
        logger.exception("failed to append strategy log path=%s", path)


__all__ = [
    "append_jsonl",
    "normalize_record_timestamps",
]
