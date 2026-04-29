"""Null-safe environment-variable and config-value parsing utilities.

Eliminates duplicated _env_bool / _env_float / _env_int / _as_bool / _as_optional_float
functions scattered across engines, risk, and config modules.
"""

from __future__ import annotations

import os
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def env_str(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default)


# ---------------------------------------------------------------------------
# Raw value coercion (payload / dict values)
# ---------------------------------------------------------------------------
def as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def as_optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_positive_int(value: Any, *, default: int = 1) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def safe_float(value: Any) -> Optional[float]:
    """Convert *value* to float, returning None for None/NaN/infinity/errors."""
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed
