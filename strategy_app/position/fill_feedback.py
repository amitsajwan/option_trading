"""Fill-truth feedback: reconcile the strategy's in-memory position against
what the broker ACTUALLY did (2026-07-10 incident: every RMS-rejected entry
left the tracker holding a phantom position that then 'traded' for hours).

Reads the same Redis fills stream the FillTracker persists, via its OWN
consumer group so the two consumers never steal each other's messages.
Fail-open everywhere: a Redis hiccup must never block the trading loop —
the N-bar timeout fallback in the engine covers missed events.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FILLS_STREAM = os.getenv("EXECUTION_FILLS_STREAM", "execution:fills:v1")
_GROUP = "strategy_fill_feedback"


class FillFeedback:
    """Non-blocking drain of broker fill events for the strategy loop."""

    def __init__(self) -> None:
        self._r = None
        self._ready = False
        try:
            import redis

            from contracts_app import redis_connection_kwargs

            self._r = redis.Redis(**redis_connection_kwargs(decode_responses=True))
            try:
                self._r.xgroup_create(_FILLS_STREAM, _GROUP, id="$", mkstream=True)
            except redis.exceptions.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            self._ready = True
        except Exception:
            logger.exception("fill feedback: init failed — feedback disabled (fail-open)")

    def drain(self) -> list[dict[str, Any]]:
        """Return all new fill events (may be empty). Never blocks, never raises."""
        if not self._ready or self._r is None:
            return []
        out: list[dict[str, Any]] = []
        try:
            msgs = self._r.xreadgroup(
                groupname=_GROUP, consumername="strategy",
                streams={_FILLS_STREAM: ">"}, count=50, block=0)
            for _stream, entries in msgs or []:
                for msg_id, fields in entries:
                    out.append(dict(fields))
                    self._r.xack(_FILLS_STREAM, _GROUP, msg_id)
        except Exception:
            logger.exception("fill feedback: drain failed (fail-open)")
        return out

    @staticmethod
    def entry_outcome(fills: list[dict[str, Any]], position_id: str) -> Optional[dict[str, Any]]:
        """The ENTRY fill event for this position, if present in the batch."""
        for f in fills:
            if (str(f.get("position_id") or "") == str(position_id)
                    and str(f.get("signal_type") or "").upper() == "ENTRY"):
                return f
        return None
