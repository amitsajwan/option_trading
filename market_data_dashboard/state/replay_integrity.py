from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _as_time_value(value: Any) -> Optional[float]:
    numeric = _as_float(value)
    if numeric is not None:
        return numeric
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        return None


def _trade_interval(item: Any) -> Optional[dict[str, Any]]:
    entry = _as_time_value(_field(item, "entryIdx"))
    exit_ = _as_time_value(_field(item, "exitIdx"))
    if entry is None:
        entry = _as_time_value(_field(item, "entry_time"))
    if exit_ is None:
        exit_ = _as_time_value(_field(item, "exit_time"))
    if entry is None or exit_ is None or exit_ <= entry:
        return None
    return {
        "id": str(_field(item, "id") or _field(item, "position_id") or ""),
        "entry": entry,
        "exit": exit_,
    }


def find_overlapping_trade_intervals(trades: list[Any]) -> list[dict[str, Any]]:
    intervals = [item for item in (_trade_interval(trade) for trade in trades) if item is not None]
    intervals.sort(key=lambda item: (float(item["entry"]), float(item["exit"]), str(item.get("id") or "")))
    overlaps: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    for current in intervals:
        active = [item for item in active if float(item["exit"]) > float(current["entry"])]
        for prior in active:
            overlaps.append(
                {
                    "first_id": prior.get("id") or None,
                    "second_id": current.get("id") or None,
                    "first_entry": prior["entry"],
                    "first_exit": prior["exit"],
                    "second_entry": current["entry"],
                    "second_exit": current["exit"],
                }
            )
        active.append(current)
    return overlaps


def replay_integrity_warnings(trades: list[Any]) -> list[str]:
    warnings: list[str] = []
    overlaps = find_overlapping_trade_intervals(trades)
    if overlaps:
        warnings.append("overlapping_replay_positions_detected")
    return warnings


__all__ = ["find_overlapping_trade_intervals", "replay_integrity_warnings"]
