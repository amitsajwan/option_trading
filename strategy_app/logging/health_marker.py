"""Health marker — a small shared-file mechanism that the strategy_app
container's healthcheck reads to determine pass/fail.

Per ARCHITECTURE.md §9, certain JSONL append failures (POSITION_OPEN/CLOSE)
are system-of-record losses that must surface as container health failures
rather than be silently logged. This module provides a minimal interface
for the signal_logger to mark those failures, plus a tiny CLI for the
healthcheck binary to query.

The marker is a JSON file at $STRATEGY_HEALTH_MARKER_PATH (default
.run/strategy_app/health_marker.json), with shape:

    {
      "ok": false,
      "failed_at_iso": "2026-05-16T11:30:00+05:30",
      "reason": "jsonl_append_failed",
      "event_type": "POSITION_OPEN",
      "details": "Errno 28 No space left on device"
    }

When health is OK, the file either doesn't exist or contains {"ok": true}.

Loose coupling: signal_logger only calls `mark_failure` / `mark_ok`.
It doesn't know how the healthcheck consumes the file. The healthcheck
binary (e.g. strategy_app/health.py) reads the file and returns the
appropriate exit code.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from contracts_app import isoformat_ist

logger = logging.getLogger(__name__)


def _default_marker_path() -> Path:
    base = os.environ.get("STRATEGY_HEALTH_MARKER_PATH")
    if base:
        return Path(base)
    run_dir = os.environ.get("STRATEGY_RUN_DIR", ".run/strategy_app")
    return Path(run_dir) / "health_marker.json"


class HealthMarker:
    """Writes / reads / clears the strategy_app health marker file.

    Construct once at process start; pass to consumers (e.g. SignalLogger)
    that need to mark failures.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _default_marker_path()

    @property
    def path(self) -> Path:
        return self._path

    def mark_failure(
        self,
        *,
        reason: str,
        event_type: str,
        details: str = "",
    ) -> None:
        """Mark health as failed. Caller of this function indicates a real,
        system-of-record loss has occurred (e.g. POSITION_OPEN JSONL append
        failed). The healthcheck will subsequently fail until mark_ok is called
        or the file is removed.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ok": False,
                "failed_at_iso": isoformat_ist(datetime.now()),
                "reason": str(reason),
                "event_type": str(event_type),
                "details": str(details)[:512],
            }
            self._path.write_text(json.dumps(payload), encoding="utf-8")
            logger.error(
                "HEALTH MARKER FAILURE: reason=%s event_type=%s details=%s",
                reason, event_type, details,
            )
        except Exception:
            # If we can't write the marker, log at error so operators see it.
            logger.exception("failed to write health marker path=%s", self._path)

    def mark_ok(self) -> None:
        """Clear any prior failure. Safe to call on every successful event;
        does nothing if the marker file doesn't exist or already says ok."""
        try:
            if self._path.exists():
                payload = {"ok": True}
                self._path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            logger.exception("failed to clear health marker path=%s", self._path)

    def is_healthy(self) -> bool:
        """Read the marker file and return health state.
        Missing file = healthy (no failures yet). Malformed file = unhealthy
        (safer than silently passing on garbage)."""
        if not self._path.exists():
            return True
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return bool(data.get("ok", False))
        except Exception:
            return False


__all__ = ["HealthMarker"]
