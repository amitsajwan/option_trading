from __future__ import annotations

import os
from typing import Optional

_MODES = {"live", "historical", "paper"}


def _resolve_mode(mode: Optional[str] = None) -> str:
    raw = str(mode or os.getenv("EXECUTION_MODE") or "live").strip().lower()
    return raw if raw in _MODES else "live"


def _is_prefixed(key: str) -> bool:
    head = str(key or "").split(":", 1)[0].strip().lower()
    return head in _MODES


def get_redis_key(key: str, mode: Optional[str] = None) -> str:
    raw = str(key or "").strip()
    if not raw:
        return raw
    if raw.startswith("system:") or _is_prefixed(raw):
        return raw
    return f"{_resolve_mode(mode)}:{raw}"


def get_redis_pattern(pattern: str, mode: Optional[str] = None) -> str:
    raw = str(pattern or "").strip()
    if not raw:
        return raw
    if raw.startswith("system:") or _is_prefixed(raw):
        return raw
    return f"{_resolve_mode(mode)}:{raw}"
