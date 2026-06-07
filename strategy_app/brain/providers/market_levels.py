"""Deterministic market reference levels — facts we compute, never recall.

Previous-day and recent-week high/low/close/return are derivable from our own
daily history, so we compute them exactly rather than asking an LLM (which
hallucinates live numbers — see docs/INTELLIGENT_BRAIN_LLM_OVERSIGHT.md). These
levels are (a) genuinely useful reference points on their own, and (b) the
*grounded facts* the morning LLM posture reasons over.

Pure module: no I/O except an optional JSON loader, deterministic for a fixed
input, fully unit-tested.

Input shape — a list of daily OHLC records::

    [{"date": "2026-06-05", "open": 53900, "high": 54210, "low": 53760, "close": 54180}, ...]

(order-independent; records missing date/high/low/close are skipped).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CONTEXT_PREFIX = "market."
_DEFAULT_FILENAME = "daily_ohlc.json"


def _as_date(raw: Any) -> Optional[date]:
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _f(raw: Any) -> Optional[float]:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v


def _clean(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep records with a valid date + high/low/close, sorted ascending by date."""
    out: list[dict[str, Any]] = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        d = _as_date(rec.get("date"))
        hi, lo, cl = _f(rec.get("high")), _f(rec.get("low")), _f(rec.get("close"))
        if d is None or hi is None or lo is None or cl is None:
            continue
        out.append({"date": d, "open": _f(rec.get("open")), "high": hi, "low": lo, "close": cl})
    out.sort(key=lambda r: r["date"])
    return out


def compute_levels(
    records: list[dict[str, Any]],
    *,
    asof: Optional[date] = None,
    week_sessions: int = 5,
) -> dict[str, Any]:
    """Compute reference levels from daily OHLC, as of (and strictly before) *asof*.

    Returns a flat dict (no prefix). Empty dict when there is no usable history.
    """
    rows = _clean(records)
    if asof is not None:
        rows = [r for r in rows if r["date"] < asof]
    if not rows:
        return {}

    prev = rows[-1]
    out: dict[str, Any] = {
        "prev_day_date": prev["date"].isoformat(),
        "prev_day_high": round(prev["high"], 4),
        "prev_day_low": round(prev["low"], 4),
        "prev_day_close": round(prev["close"], 4),
    }
    if prev["close"] > 0:
        out["prev_day_range_pct"] = round((prev["high"] - prev["low"]) / prev["close"], 6)

    window = rows[-max(1, int(week_sessions)):]
    out["week_sessions"] = len(window)
    out["recent_week_high"] = round(max(r["high"] for r in window), 4)
    out["recent_week_low"] = round(min(r["low"] for r in window), 4)
    first_open = window[0].get("open") or window[0]["close"]
    if first_open and first_open > 0:
        out["recent_week_return_pct"] = round((window[-1]["close"] - first_open) / first_open, 6)

    return out


def prefixed_levels(records: list[dict[str, Any]], *, asof: Optional[date] = None) -> dict[str, Any]:
    """compute_levels with the ``market.`` context prefix applied."""
    return {f"{_CONTEXT_PREFIX}{k}": v for k, v in compute_levels(records, asof=asof).items()}


def daily_ohlc_path() -> Path:
    explicit = os.getenv("BRAIN_MARKET_OHLC_PATH", "").strip()
    if explicit:
        return Path(explicit)
    run_dir = (
        os.getenv("STRATEGY_RUNTIME_ARTIFACT_DIR")
        or os.getenv("STRATEGY_RUN_DIR")
        or ".run/strategy_app"
    )
    return Path(run_dir) / _DEFAULT_FILENAME


def load_daily_ohlc(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Load daily OHLC from JSON (list of records, or date-keyed dict). [] if absent."""
    p = path if path is not None else daily_ohlc_path()
    if not p.exists():
        logger.debug("daily_ohlc file not found path=%s", p)
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("daily_ohlc load failed path=%s error=%s", p, exc)
        return []
    if isinstance(raw, dict):
        # the dict key is the authoritative date — let it win over any stale
        # "date" field inside the value.
        return [{**v, "date": k} for k, v in raw.items() if isinstance(v, dict)]
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    logger.warning("daily_ohlc unexpected format path=%s", p)
    return []


__all__ = [
    "compute_levels",
    "prefixed_levels",
    "load_daily_ohlc",
    "daily_ohlc_path",
]
