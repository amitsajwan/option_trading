"""JSONL-first reader for the *currently-running* strategy_app session.

Backs the `/api/strategy/current/*` endpoints. Reads directly from the
canonical JSONL files written by `strategy_app.logging.signal_logger` and
the `health_marker.json` written when a critical append fails.

Per ARCHITECTURE.md §9 the split-by-query-type rule:
- current run / current session  → JSONL (this module)
- cross-day aggregates           → MongoDB (existing services)

This module intentionally does NO mongo work; pure filesystem reads so that
the endpoint surfaces correct data even when the mongo persistence path is
slow or unavailable.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


DEFAULT_HISTORICAL_RUN_DIR = Path("/app/.run/strategy_app_historical")
DEFAULT_LIVE_RUN_DIR = Path("/app/.run/strategy_app")


@dataclass
class _CurrentState:
    mode: str
    run_id: Optional[str]
    jsonl_path: str
    file_exists: bool
    file_size_bytes: int
    health_marker: dict[str, Any]
    stats: dict[str, Any]
    latest_positions: list[dict[str, Any]]


def _resolve_run_dir(mode: str) -> Path:
    """Return the on-disk run_dir for the given mode.

    Order of precedence:
      1. Explicit env override (`STRATEGY_RUN_DIR_LIVE` / `STRATEGY_RUN_DIR_HISTORICAL`)
      2. Default by mode
    """
    mode = mode.strip().lower()
    if mode in {"historical", "replay"}:
        return Path(os.getenv("STRATEGY_RUN_DIR_HISTORICAL") or DEFAULT_HISTORICAL_RUN_DIR)
    return Path(os.getenv("STRATEGY_RUN_DIR_LIVE") or DEFAULT_LIVE_RUN_DIR)


def _tail_lines(path: Path, n: int = 50, max_bytes_back: int = 2_000_000) -> list[str]:
    """Read the last `n` lines from a (potentially very large) JSONL file
    efficiently — seek to near-end, scan backwards.

    `max_bytes_back` is a safety cap to avoid pathological I/O on truly
    huge files. For our positions.jsonl files (typically < 50 MB) the
    cap is never reached.
    """
    if not path.exists():
        return []
    size = path.stat().st_size
    if size == 0:
        return []
    read_from = max(0, size - max_bytes_back)
    try:
        with path.open("rb") as f:
            f.seek(read_from)
            tail = f.read()
    except Exception:
        return []
    text = tail.decode("utf-8", errors="replace")
    # If we didn't start at byte 0, the first partial line is incomplete — drop it.
    if read_from > 0 and "\n" in text:
        text = text.split("\n", 1)[1]
    # Use splitlines() so we handle \n, \r\n, and \r consistently.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Yield parsed JSON dicts from a JSONL file. Skips malformed lines."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _read_health_marker(path: Path) -> dict[str, Any]:
    """Read the JSONL health marker file. Missing file = healthy.
    Malformed file = unhealthy (fail-safe), matching HealthMarker.is_healthy."""
    if not path.exists():
        return {"ok": True}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "reason": "marker_unreadable"}


def _compute_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up summary counters across all events in the JSONL."""
    if not records:
        return {
            "total_records": 0,
            "first_event_at": None,
            "last_event_at": None,
            "event_counts": {},
            "run_ids_seen": [],
            "current_run_id": None,
        }
    event_counts: Counter[str] = Counter()
    run_ids: list[str] = []
    first_ts = None
    last_ts = None
    for r in records:
        event_counts[str(r.get("event") or "?")] += 1
        ts = r.get("timestamp") or r.get("entry_time")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        run = r.get("run_id")
        if run and (not run_ids or run_ids[-1] != run):
            run_ids.append(run)
    return {
        "total_records": len(records),
        "first_event_at": first_ts,
        "last_event_at": last_ts,
        "event_counts": dict(event_counts),
        "run_ids_seen": run_ids,
        "current_run_id": run_ids[-1] if run_ids else None,
    }


def read_strategy_current_state(
    mode: str = "live",
    *,
    latest_n: int = 50,
    run_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Build a summary of the currently-running strategy_app session.

    `mode` selects which on-disk run_dir to read. `latest_n` controls how many
    recent position events to include in the response. The call is read-only
    and does NOT touch mongo.
    """
    run_dir_path = Path(run_dir) if run_dir else _resolve_run_dir(mode)
    positions_path = run_dir_path / "positions.jsonl"
    marker_path = run_dir_path / "health_marker.json"

    file_exists = positions_path.exists()
    file_size = positions_path.stat().st_size if file_exists else 0

    # For the stats roll-up we read every record (one full file pass). For most
    # of our positions.jsonl this is fast (<50 MB). For latest_n we use the
    # efficient tail to avoid double-parsing.
    all_records: list[dict[str, Any]] = list(_iter_jsonl(positions_path)) if file_exists else []
    stats = _compute_stats(all_records)

    latest_position_records: list[dict[str, Any]] = []
    if file_exists:
        for raw_line in _tail_lines(positions_path, n=latest_n):
            try:
                latest_position_records.append(json.loads(raw_line))
            except json.JSONDecodeError:
                continue

    health_marker = _read_health_marker(marker_path)

    state = _CurrentState(
        mode=mode.strip().lower(),
        run_id=stats.get("current_run_id"),
        jsonl_path=str(positions_path),
        file_exists=file_exists,
        file_size_bytes=file_size,
        health_marker=health_marker,
        stats=stats,
        latest_positions=latest_position_records,
    )
    return asdict(state)


__all__ = ["read_strategy_current_state"]
