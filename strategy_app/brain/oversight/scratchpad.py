"""MEMORY + SCRATCHPAD — the oversight brain's running day-state across cycles.

Holds the running thesis and a log of each cycle's verdict + the facts behind it,
persisted to a per-day JSONL so the brain (and later, scoring) can read its own
history. ``to_prompt_context()`` is the summarized memory handed back to the LLM
next cycle so it can keep — or deliberately flip — its thesis.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class Scratchpad:
    trade_date: str
    thesis: str = ""
    cycles: list[dict[str, Any]] = field(default_factory=list)

    def add(self, *, time: str, verdict: Any, facts: dict[str, Any]) -> None:
        """Append one cycle's outcome and advance the running thesis."""
        self.cycles.append(
            {
                "time": time,
                "posture": verdict.posture,
                "lean": verdict.direction_lean,
                "lean_conf": round(float(verdict.lean_confidence), 3),
                "risk_flag": verdict.risk_flag,
                "thesis": verdict.thesis,
                "reasoning": verdict.reasoning,
                "facts": facts,
            }
        )
        if verdict.thesis:
            self.thesis = verdict.thesis

    def recent(self, n: int = 4) -> list[dict[str, Any]]:
        return self.cycles[-n:]

    def to_prompt_context(self, n: int = 4) -> dict[str, Any]:
        """Summarized memory for the next LLM cycle (thesis + recent leans/notes)."""
        return {
            "running_thesis": self.thesis,
            "recent_cycles": [
                {
                    "time": c.get("time"),
                    "posture": c.get("posture"),
                    "lean": c.get("lean"),
                    "risk_flag": c.get("risk_flag"),
                    "note": str(c.get("reasoning") or "")[:160],
                }
                for c in self.cycles[-n:]
            ],
        }

    # ── persistence ──────────────────────────────────────────────────────────
    def persist(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"trade_date": self.trade_date, "thesis": self.thesis, "cycles": self.cycles}),
                encoding="utf-8",
            )
        except Exception as exc:  # memory is best-effort
            logger.warning("scratchpad persist failed path=%s error=%s", path, exc)

    @classmethod
    def load_or_new(cls, path: Path, trade_date: str) -> "Scratchpad":
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and str(raw.get("trade_date")) == str(trade_date):
                    return cls(
                        trade_date=trade_date,
                        thesis=str(raw.get("thesis") or ""),
                        cycles=list(raw.get("cycles") or []),
                    )
        except Exception as exc:
            logger.warning("scratchpad load failed path=%s error=%s", path, exc)
        return cls(trade_date=str(trade_date))


__all__ = ["Scratchpad"]
