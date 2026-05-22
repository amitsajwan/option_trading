"""JSONL-backed cross-session memory.

Writes one record per closed session to ``session_summary.jsonl`` in the
strategy run directory.  On session start, SessionMemory.load_carry() reads
the most recent record and returns a SessionCarry that the brain uses to
initialise risk state with yesterday's context.

This is intentionally append-only (same pattern as the existing JSONL
trade/signal logs).  No records are ever modified or deleted.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .context import SessionCarry

logger = logging.getLogger(__name__)

_DEFAULT_MEMORY_DIR = Path(".run") / "strategy_app"
_SUMMARY_FILENAME = "session_summary.jsonl"


def _memory_dir() -> Path:
    env = os.getenv("STRATEGY_RUNTIME_ARTIFACT_DIR") or os.getenv("STRATEGY_RUN_DIR")
    return Path(env).resolve() if env else _DEFAULT_MEMORY_DIR.resolve()


class SessionMemory:
    """Reads and writes per-session summaries.

    Thread-safety: not required — only one strategy_app process writes.
    """

    def __init__(self, memory_dir: Optional[Path] = None) -> None:
        self._dir = Path(memory_dir) if memory_dir else _memory_dir()
        self._path = self._dir / _SUMMARY_FILENAME

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load_carry(self, as_of_date: date) -> SessionCarry:
        """Return SessionCarry from the most recent session before *as_of_date*.

        Falls back to SessionCarry.empty() if the file does not exist or
        contains no usable records.
        """
        if not self._path.exists():
            return SessionCarry.empty()
        try:
            records = self._read_all()
        except Exception as exc:
            logger.warning("session_memory read failed path=%s error=%s", self._path, exc)
            return SessionCarry.empty()

        # Find the most recent record strictly before as_of_date
        best: Optional[dict[str, Any]] = None
        for rec in records:
            raw_date = rec.get("trade_date")
            if not raw_date:
                continue
            try:
                rec_date = date.fromisoformat(str(raw_date))
            except (ValueError, TypeError):
                continue
            if rec_date >= as_of_date:
                continue
            best_date = date.fromisoformat(str(best["trade_date"])) if best is not None else None
            if best_date is None or rec_date > best_date:
                best = rec

        if best is None:
            return SessionCarry.empty()

        return self._record_to_carry(best)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_summary(
        self,
        *,
        trade_date: date,
        trades: int,
        wins: int,
        losses: int,
        consecutive_losses: int,
        session_pnl_pct: float,
    ) -> None:
        """Append a session summary record.  Called at on_session_end."""
        carry = self.load_carry(trade_date)
        losing_streak = (
            carry.losing_streak_days + 1
            if consecutive_losses > 0 and session_pnl_pct < 0
            else 0
        )
        record: dict[str, Any] = {
            "trade_date": trade_date.isoformat(),
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "consecutive_losses_at_close": consecutive_losses,
            "session_pnl_pct": round(float(session_pnl_pct), 6),
            "losing_streak_days": losing_streak,
        }
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            logger.info(
                "session_memory saved trade_date=%s trades=%d pnl=%.4f%% consec_losses=%d",
                trade_date.isoformat(),
                trades,
                session_pnl_pct * 100.0,
                consecutive_losses,
            )
        except Exception as exc:
            logger.warning(
                "session_memory write failed path=%s error=%s", self._path, exc
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_all(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    @staticmethod
    def _record_to_carry(rec: dict[str, Any]) -> SessionCarry:
        raw_date = rec.get("trade_date")
        last_date: Optional[date] = None
        if raw_date:
            try:
                last_date = date.fromisoformat(str(raw_date))
            except (ValueError, TypeError):
                pass
        return SessionCarry(
            consecutive_losses_at_close=int(rec.get("consecutive_losses_at_close", 0)),
            prior_day_pnl_pct=float(rec.get("session_pnl_pct", 0.0)),
            prior_week_pnl_pct=0.0,  # computed lazily from multi-record read if needed
            losing_streak_days=int(rec.get("losing_streak_days", 0)),
            last_trade_date=last_date,
        )


__all__ = ["SessionMemory"]
