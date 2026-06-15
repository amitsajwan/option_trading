"""Typed config access (Phase 5 foundation).

The 404 existing ``os.getenv()`` call sites keep working unchanged (the loader
populates os.environ). This module gives NEW code a typed, grouped accessor so it
can read ``view().exit.lottery.hard_stop_pct`` (a float) instead of
``float(os.getenv("LOTTERY_HARD_STOP_PCT", "0.20"))``.

Values are read from ``os.environ`` at access time, so SIM overrides and the
loader both apply. Types come from the registry (float/int/bool/csv/str).

Migration is incremental and optional — adopt in new/touched code; there is no
need (or plan) to rewrite all call sites at once.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from .registry import BY_ENV, BY_YAML, ConfigKey


def _coerce(key: ConfigKey, raw: str | None) -> Any:
    if raw is None or raw == "":
        raw = key.format(key.default)
    if key.type == "bool":
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}
    if key.type == "int":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return key.default
    if key.type == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return key.default
    if key.type == "csv":
        return [p.strip() for p in str(raw).split(",") if p.strip()]
    return str(raw)


def value(key_or_path: str) -> Any:
    """Typed value for an env var OR a dotted yaml path. Reads os.environ now."""
    key = BY_ENV.get(key_or_path) or BY_YAML.get(key_or_path)
    if key is None:
        raise KeyError(f"unknown config key: {key_or_path!r}")
    return _coerce(key, os.environ.get(key.env_var))


def view() -> SimpleNamespace:
    """Nested, typed snapshot of all config — ``view().exit.lottery.hard_stop_pct``."""
    root: dict[str, Any] = {}
    for key in BY_YAML.values():
        parts = key.yaml_path.split(".")
        cur = root
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = _coerce(key, os.environ.get(key.env_var))

    def to_ns(d: dict) -> SimpleNamespace:
        return SimpleNamespace(**{k: to_ns(v) if isinstance(v, dict) else v for k, v in d.items()})

    return to_ns(root)
