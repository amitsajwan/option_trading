"""Read TradingBrain morning context from brain_state.json.

Written by DeterministicRuleEngine._write_brain_state() at session start.
Consumed by GET /api/strategy/brain/status.

File location: {STRATEGY_RUN_DIR}/brain_state.json
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def read_brain_state(mode: str = "live") -> dict[str, Any]:
    """Return brain morning context for the given mode.

    Response shape::

        {
          "available": true,
          "mode": "live",
          "brain_state_path": "/app/.run/strategy_app/brain_state.json",
          "trade_date": "2026-05-21",
          "brain_enabled": true,
          "day_score": "CALM",
          "day_score_confidence": 0.8,
          "day_score_reason": "daily_features:CALM+rv20=0.0085",
          "regime_rv20": 0.0085,
          "regime_dist_sma20": 0.012,
          "regime_sma20_slope": 0.0003,
          "regime_60d_return": 0.07,
          "vix_level": null,
          "size_multiplier": 1.0,
          "carry_consecutive_losses": 0,
          "losing_streak_days": 0,
          "prior_day_pnl_pct": 0.023,
          "checked_at_ist": "2026-05-21T08:45:12+05:30"
        }

    Returns ``{available: false, ...}`` when brain_state.json does not exist.
    """
    run_dir = _run_dir(mode)
    brain_state_path = run_dir / "brain_state.json"
    checked_at = isoformat_ist(datetime.now(tz=timezone.utc))

    base: dict[str, Any] = {
        "available": False,
        "mode": mode.strip().lower(),
        "brain_state_path": str(brain_state_path),
        "checked_at_ist": checked_at,
    }

    if not brain_state_path.exists():
        base["reason"] = "brain_state.json not found — engine not started or brain disabled"
        return base

    try:
        raw = json.loads(brain_state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        base["reason"] = f"read error: {exc}"
        return base

    if not isinstance(raw, dict):
        base["reason"] = "unexpected format"
        return base

    ctx = raw.get("day_context") or {}
    carry = ctx.get("session_carry") or {}

    result: dict[str, Any] = {
        "available": True,
        "mode": mode.strip().lower(),
        "brain_state_path": str(brain_state_path),
        "checked_at_ist": checked_at,
        "trade_date": raw.get("trade_date"),
        "brain_enabled": bool(raw.get("brain_enabled", True)),
        "day_score": ctx.get("day_score", "UNKNOWN"),
        "day_score_confidence": ctx.get("day_score_confidence"),
        "day_score_reason": ctx.get("day_score_reason"),
        "regime_rv20": ctx.get("regime_rv20"),
        "regime_dist_sma20": ctx.get("regime_dist_sma20"),
        "regime_sma20_slope": ctx.get("regime_sma20_slope"),
        "regime_60d_return": ctx.get("regime_60d_return"),
        "vix_level": ctx.get("vix_level"),
        "size_multiplier": ctx.get("size_multiplier", 1.0),
        "carry_consecutive_losses": carry.get("consecutive_losses_at_close", 0),
        "losing_streak_days": carry.get("losing_streak_days", 0),
        "prior_day_pnl_pct": carry.get("prior_day_pnl_pct"),
        "last_trade_date": carry.get("last_trade_date"),
    }
    return result


__all__ = ["read_brain_state"]
