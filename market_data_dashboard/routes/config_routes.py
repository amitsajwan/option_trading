"""GET /api/config/effective — effective strategy config as seen by the running process.

Combines two sources:
  1. runtime_config.json — engine, profile, rollout (written by strategy_app at startup)
  2. os.environ — model-specific thresholds (ENTRY_ML_MIN_PROB, DIRECTION_ML_MODEL_PATH, …)

This is the answer to "what is actually running right now" without SSH.
The instrument parameter scopes to the matching strategy_app instance
(single-instance today; multi-instance when NIFTY is deployed).
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Query

try:
    from ..state.strategy_current_state import _resolve_run_dir
except ImportError:
    from market_data_dashboard.state.strategy_current_state import _resolve_run_dir  # type: ignore

logger = logging.getLogger(__name__)
_IST = timezone(timedelta(hours=5, minutes=30))

# Env vars that matter for strategy operation — exposed verbatim.
_STRATEGY_ENV_KEYS = [
    "ENTRY_ML_MODEL_PATH",
    "ENTRY_ML_MIN_PROB",
    "ML_ENTRY_DIRECTION_MODE",
    "DIRECTION_ML_MODEL_PATH",
    "STRATEGY_MIN_CONFIDENCE",
    "STRATEGY_PROFILE_ID",
    "EXIT_STRATEGY_MODE",
    "EXIT_MAX_LOSS_PCT",
    "EXIT_SCALPER_HARD_STOP_PCT",
    "ENTRY_TIME_WINDOWS",
    "EXIT_POLICY_STACK_ENABLED",
    "ROLLOUT_STAGE",
    "STRATEGY_ROLLOUT_STAGE",
    "STRATEGY_ENGINE",
    "STRATEGY_INSTRUMENT",
    "NIFTY_ENTRY_ML_MODEL_PATH",
    "NIFTY_DIRECTION_ML_MODEL_PATH",
]


def _model_file_status(path_str: Optional[str]) -> dict[str, Any]:
    if not path_str:
        return {"path": None, "exists": False}
    p = pathlib.Path(path_str)
    return {
        "path": path_str,
        "exists": p.exists(),
        "size_kb": round(p.stat().st_size / 1024, 1) if p.exists() else None,
    }


def _load_runtime_config(mode: str = "live", instrument: str = "BANKNIFTY") -> dict[str, Any]:
    resolved_mode = "nifty" if instrument.upper() == "NIFTY" else mode
    run_dir = _resolve_run_dir(resolved_mode)
    cfg_path = run_dir / "runtime_config.json"
    if not cfg_path.exists():
        return {"status": "missing", "path": str(cfg_path)}
    try:
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        return {"status": "ok", "path": str(cfg_path), "payload": payload}
    except Exception as exc:
        return {"status": "error", "path": str(cfg_path), "error": str(exc)}


class ConfigRouter:
    """GET /api/config/effective — live strategy config (env + runtime_config)."""

    def __init__(self) -> None:
        router = APIRouter(tags=["config"])
        router.add_api_route("/api/config/effective", self.get_effective_config, methods=["GET"])
        self.router = router

    async def get_effective_config(
        self,
        instrument: str = Query("BANKNIFTY", description="BANKNIFTY | NIFTY"),
        mode: str = Query("live", description="live | replay"),
    ) -> dict[str, Any]:
        now_ist = datetime.now(tz=_IST).isoformat()

        # 1. Runtime config file — scoped to the correct instrument's run dir
        rc = _load_runtime_config(mode, instrument)
        rc_payload = rc.get("payload") or {}

        # 2. Env vars
        env_vals: dict[str, Optional[str]] = {k: os.getenv(k) for k in _STRATEGY_ENV_KEYS}

        # 3. Entry model file check — use instrument-specific paths for NIFTY
        is_nifty = instrument.upper() == "NIFTY"
        entry_path = (
            env_vals.get("NIFTY_ENTRY_ML_MODEL_PATH") or ""
            if is_nifty
            else env_vals.get("ENTRY_ML_MODEL_PATH") or ""
        )
        dir_path = (
            env_vals.get("NIFTY_DIRECTION_ML_MODEL_PATH") or ""
            if is_nifty
            else env_vals.get("DIRECTION_ML_MODEL_PATH") or ""
        )

        # 4. Compose effective view
        engine = rc_payload.get("engine") or os.getenv("STRATEGY_ENGINE", "")
        profile = (
            rc_payload.get("strategy_profile_id")
            or env_vals.get("STRATEGY_PROFILE_ID")
            or ""
        )

        return {
            "instrument": instrument.upper(),
            "mode": mode,
            "checked_at_ist": now_ist,

            # ── Engine ────────────────────────────────────────────────────────
            "engine": engine,
            "strategy_profile_id": profile,
            "rollout_stage": (
                (rc_payload.get("rollout") or {}).get("stage")
                or env_vals.get("ROLLOUT_STAGE")
                or env_vals.get("STRATEGY_ROLLOUT_STAGE")
            ),

            # ── Entry model ───────────────────────────────────────────────────
            "entry_model": _model_file_status(entry_path),
            "entry_ml_min_prob": _safe_float(env_vals.get("ENTRY_ML_MIN_PROB")),
            "entry_time_windows": env_vals.get("ENTRY_TIME_WINDOWS"),

            # ── Direction ─────────────────────────────────────────────────────
            "direction_mode": env_vals.get("ML_ENTRY_DIRECTION_MODE"),
            "direction_model": _model_file_status(dir_path),

            # ── Confidence / risk ─────────────────────────────────────────────
            "strategy_min_confidence": _safe_float(env_vals.get("STRATEGY_MIN_CONFIDENCE")),
            "exit_strategy_mode": env_vals.get("EXIT_STRATEGY_MODE"),
            "exit_max_loss_pct": _safe_float(env_vals.get("EXIT_MAX_LOSS_PCT")),
            "exit_scalper_hard_stop_pct": _safe_float(env_vals.get("EXIT_SCALPER_HARD_STOP_PCT")),
            "exit_policy_stack_enabled": _truthy(env_vals.get("EXIT_POLICY_STACK_ENABLED")),

            # ── Runtime config file ───────────────────────────────────────────
            "runtime_config_status": rc.get("status"),
            "runtime_config_path": rc.get("path"),
            "runtime_config_started_at": rc_payload.get("checked_at_ist"),

            # ── All raw env vars (for debugging) ─────────────────────────────
            "env": env_vals,
        }


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _truthy(v: Any) -> Optional[bool]:
    if v is None:
        return None
    return str(v).strip().lower() in ("1", "true", "yes", "on")
