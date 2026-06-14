"""Read the LLM direction-shadow log written by strategy_app.

strategy_app/brain/direction_shadow.py appends one JSONL line per entry with what the
Groq/Gemini direction advisor *would* have picked (shadow only — never affects orders).
This surfaces it at GET /api/strategy/brain/direction-shadow so the LLM-vs-our-direction
experiment can be watched live on paper.

Self-contained (no strategy_app import), same run-dir convention as brain_state.py.
File: {STRATEGY_RUN_DIR}/direction_shadow.jsonl
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from contracts_app import isoformat_ist

_DEFAULT_LIVE_DIR = Path("/app/.run/strategy_app")
_DEFAULT_HISTORICAL_DIR = Path("/app/.run/strategy_app_historical")


def _run_dir(mode: str) -> Path:
    mode = mode.strip().lower()
    if mode in {"historical", "replay"}:
        env = os.getenv("STRATEGY_RUN_DIR_HISTORICAL", "")
        return Path(env) if env else _DEFAULT_HISTORICAL_DIR
    env = os.getenv("STRATEGY_RUN_DIR_LIVE", "")
    return Path(env) if env else _DEFAULT_LIVE_DIR


def _rate(a: int, b: int) -> Optional[float]:
    return round(a / b, 4) if b else None


def read_direction_shadow(mode: str = "live", recent: int = 20) -> dict[str, Any]:
    """Summarise the direction-shadow JSONL for the given mode.

    Truth-free metrics (commit rate, agreement with our composite, confidence, grounded
    rate) are always available. LLM accuracy appears only once outcomes are backfilled
    (a ``truth`` field) by tools/reconcile_direction_shadow.py.
    """
    path = _run_dir(mode) / "direction_shadow.jsonl"
    checked_at = isoformat_ist(datetime.now(tz=timezone.utc))
    base: dict[str, Any] = {
        "available": False, "mode": mode.strip().lower(),
        "path": str(path), "checked_at_ist": checked_at, "n": 0,
    }
    if not path.exists():
        base["reason"] = "direction_shadow.jsonl not found — DIRECTION_SHADOW_ENABLED off or no entries yet"
        return base

    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        base["reason"] = f"read error: {exc}"
        return base

    n = len(rows)
    committed = [r for r in rows if r.get("llm_direction") in ("CE", "PE")]
    errs = [r for r in rows if r.get("llm_error")]
    agreed = [r for r in committed if r.get("agrees_taken") is True]
    grounded = [r for r in rows if r.get("llm_grounded")]
    confs = [float(r["llm_confidence"]) for r in committed
             if isinstance(r.get("llm_confidence"), (int, float))]
    truthed = [r for r in committed if r.get("truth") in ("CE", "PE")]
    correct = [r for r in truthed if r.get("llm_direction") == r.get("truth")]

    return {
        "available": True, "mode": mode.strip().lower(), "path": str(path),
        "checked_at_ist": checked_at,
        "n": n,
        "n_committed": len(committed),
        "n_abstain": n - len(committed) - len(errs),
        "n_errors": len(errs),
        "commit_rate": _rate(len(committed), n),
        "agree_with_taken_rate": _rate(len(agreed), len(committed)),
        "grounded_rate": _rate(len(grounded), n),
        "mean_confidence": round(sum(confs) / len(confs), 4) if confs else None,
        "llm_accuracy_if_truthed": _rate(len(correct), len(truthed)),
        "n_truthed": len(truthed),
        "recent": rows[-recent:],
    }


__all__ = ["read_direction_shadow"]
