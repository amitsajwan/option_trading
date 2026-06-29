"""GET /api/instruments — multi-instrument status aggregator.

Returns live status for every active instrument (BANKNIFTY, NIFTY, …):
  mode (live|sim|off), model health, feed staleness, today's trades/P&L,
  current regime, and expiry context.

Data sources (all read-only):
  - runtime_config.json  → engine + model paths
  - strategy_decision_traces (mongo) → regime + last bar time
  - strategy_positions (mongo)       → today's trades + P&L
  - Redis system:feed:last_tick:{instrument} → feed staleness
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter

try:
    from .._namespace import BASE_DECISION_TRACES, BASE_POSITIONS, collection_for
    from ..real_source import make_mongo_db
    from ..state.strategy_current_state import _resolve_run_dir
except ImportError:
    from market_data_dashboard._namespace import BASE_DECISION_TRACES, BASE_POSITIONS, collection_for  # type: ignore
    from market_data_dashboard.real_source import make_mongo_db  # type: ignore
    from market_data_dashboard.state.strategy_current_state import _resolve_run_dir  # type: ignore

import redis as _redis_lib

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Instruments known to the system — extend when NIFTY is deployed.
_KNOWN_INSTRUMENTS = ["BANKNIFTY", "NIFTY"]

# Expiry cadence for DTE calculation.
_EXPIRY_CADENCE: dict[str, str] = {
    "BANKNIFTY": "monthly",   # post-Nov 2024: last Thursday of month
    "NIFTY":     "weekly",    # every Thursday
}


def _now_ist() -> datetime:
    return datetime.now(tz=_IST)


def _today_ist() -> str:
    return _now_ist().strftime("%Y-%m-%d")


def _make_redis() -> Any:
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    try:
        r = _redis_lib.Redis(host=host, port=port, db=0, socket_timeout=1, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _feed_last_tick_age_sec(r: Any, instrument: str) -> Optional[int]:
    """Seconds since the last tick was received for this instrument.

    strategy_app / ingestion_app writes system:feed:last_tick:{instrument}
    as a Unix-ms timestamp string. Falls back to None if Redis unavailable
    or key missing.
    """
    if r is None:
        return None
    try:
        val = r.get(f"system:feed:last_tick:{instrument}")
        if val is None:
            # Also try without instrument (single-instrument legacy key)
            val = r.get("system:feed:last_tick")
        if val is None:
            return None
        ts_ms = float(val)
        now_ms = datetime.now(tz=timezone.utc).timestamp() * 1000
        return max(0, int((now_ms - ts_ms) / 1000))
    except Exception:
        return None


def _model_loaded(run_dir_mode: str = "live") -> dict[str, bool]:
    """Check runtime_config.json for model load status."""
    try:
        run_dir = _resolve_run_dir(run_dir_mode)
        cfg_path = run_dir / "runtime_config.json"
        if not cfg_path.exists():
            return {"entry": False, "direction": False}
        import json
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        engine = str(cfg.get("engine") or "")
        if engine == "deterministic":
            # Deterministic engine: check env vars for model paths
            entry_path = os.getenv("ENTRY_ML_MODEL_PATH", "")
            dir_path = os.getenv("DIRECTION_ML_MODEL_PATH", "")
            import pathlib
            return {
                "entry": bool(entry_path) and pathlib.Path(entry_path).exists(),
                "direction": bool(dir_path) and pathlib.Path(dir_path).exists(),
            }
        # ml_pure / staged: model loaded if runtime_config has a run_id
        model = cfg.get("model") or {}
        has_run_id = bool(model.get("run_id") or model.get("model_package_path"))
        return {"entry": has_run_id, "direction": False}
    except Exception:
        return {"entry": False, "direction": False}


def _today_stats(db: Any, instrument: str, today: str) -> dict[str, Any]:
    """Query today's closed positions for trade count and P&L."""
    try:
        coll = db[collection_for(BASE_POSITIONS, kind="live", instrument=instrument)]
        docs = list(coll.find(
            {
                "event": "POSITION_CLOSE",
                "trade_date_ist": today,
            },
            {"pnl_pct": 1, "actual_return_pct": 1, "_id": 0},
        ))
        count = len(docs)
        pnls = [
            float(d.get("actual_return_pct") or d.get("pnl_pct") or 0)
            for d in docs
        ]
        total_pnl = sum(pnls)
        return {"today_trades": count, "today_pnl_pct": round(total_pnl, 4)}
    except Exception:
        return {"today_trades": 0, "today_pnl_pct": 0.0}


def _latest_regime(db: Any, instrument: str, today: str) -> Optional[str]:
    """Most recent regime from today's decision traces."""
    try:
        coll = db[collection_for(BASE_DECISION_TRACES, kind="live", instrument=instrument)]
        doc = coll.find_one(
            {"trade_date_ist": today},
            sort=[("timestamp", -1)],
            projection={"payload.regime": 1, "payload.regime_context.regime": 1, "_id": 0},
        )
        if not doc:
            return None
        payload = doc.get("payload") or {}
        regime = (
            payload.get("regime")
            or (payload.get("regime_context") or {}).get("regime")
        )
        return str(regime) if regime else None
    except Exception:
        return None


def _next_expiry(instrument: str, today_dt: datetime) -> Optional[str]:
    """Calculate next expiry date for the instrument.

    NIFTY: next Thursday (weekly).
    BANKNIFTY: last Thursday of current month (monthly post-Nov-2024).
    Returns ISO date string or None on error.
    """
    try:
        from datetime import date
        today = today_dt.date()
        cadence = _EXPIRY_CADENCE.get(instrument, "weekly")

        if cadence == "weekly":
            # Next Thursday (weekday=3)
            days_ahead = (3 - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # today is Thursday → use next Thursday
            expiry = today + timedelta(days=days_ahead)
        else:
            # Monthly: last Thursday of current month
            import calendar
            year, month = today.year, today.month
            # Find last Thursday in month
            last_day = calendar.monthrange(year, month)[1]
            last_thu = None
            for d in range(last_day, 0, -1):
                if date(year, month, d).weekday() == 3:
                    last_thu = date(year, month, d)
                    break
            if last_thu is None or last_thu < today:
                # Move to next month
                if month == 12:
                    year, month = year + 1, 1
                else:
                    month += 1
                last_day = calendar.monthrange(year, month)[1]
                for d in range(last_day, 0, -1):
                    if date(year, month, d).weekday() == 3:
                        last_thu = date(year, month, d)
                        break
            expiry = last_thu

        dte = (expiry - today).days
        return expiry.isoformat(), dte
    except Exception:
        return None, None


def _instrument_mode(instrument: str) -> str:
    """Determine mode from env vars.

    STRATEGY_MODE env var or ROLLOUT_STAGE → live|sim|off.
    Single-instrument setup: if instrument matches current subscription → live/sim.
    """
    rollout = str(os.getenv("ROLLOUT_STAGE", "") or os.getenv("STRATEGY_ROLLOUT_STAGE", "")).lower()
    if rollout in ("live", "capped_live", "paper"):
        mode = "live" if "live" in rollout else "sim"
    else:
        mode = "sim"

    # If NIFTY is not yet deployed (no model paths), report as off
    if instrument == "NIFTY":
        nifty_entry = os.getenv("NIFTY_ENTRY_ML_MODEL_PATH", "")
        bn_only = not bool(nifty_entry)
        if bn_only:
            return "off"
    return mode


class InstrumentsRouter:
    """GET /api/instruments — live status for all active instruments."""

    def __init__(self) -> None:
        router = APIRouter(tags=["instruments"])
        router.add_api_route("/api/instruments", self.get_instruments, methods=["GET"])
        router.add_api_route("/api/instruments/{instrument}", self.get_instrument, methods=["GET"])
        self.router = router

    async def get_instruments(self) -> list[dict[str, Any]]:
        return _build_all_statuses()

    async def get_instrument(self, instrument: str) -> dict[str, Any]:
        status = _build_instrument_status(instrument.upper())
        return status


def _build_all_statuses() -> list[dict[str, Any]]:
    try:
        db = make_mongo_db()
    except Exception:
        db = None
    r = _make_redis()
    now = _now_ist()
    today = now.strftime("%Y-%m-%d")
    result = []
    for instrument in _KNOWN_INSTRUMENTS:
        result.append(_build_one(instrument, db, r, now, today))
    return result


def _build_instrument_status(instrument: str) -> dict[str, Any]:
    try:
        db = make_mongo_db()
    except Exception:
        db = None
    r = _make_redis()
    now = _now_ist()
    today = now.strftime("%Y-%m-%d")
    return _build_one(instrument, db, r, now, today)


def _build_one(instrument: str, db: Any, r: Any, now: datetime, today: str) -> dict[str, Any]:
    mode = _instrument_mode(instrument)
    models = _model_loaded("live") if instrument == "BANKNIFTY" else {"entry": False, "direction": False}
    feed_age = _feed_last_tick_age_sec(r, instrument)
    stats = _today_stats(db, instrument, today) if db is not None else {"today_trades": 0, "today_pnl_pct": 0.0}
    regime = _latest_regime(db, instrument, today) if db is not None else None
    expiry, dte = _next_expiry(instrument, now)
    return {
        "id": instrument,
        "mode": mode,
        "model_entry_loaded": models["entry"],
        "model_direction_loaded": models["direction"],
        "feed_last_tick_age_sec": feed_age,
        "feed_stale": feed_age is not None and feed_age > 120,
        "today_trades": stats["today_trades"],
        "today_pnl_pct": stats["today_pnl_pct"],
        "regime": regime,
        "current_expiry": expiry,
        "dte": dte,
        "expiry_cadence": _EXPIRY_CADENCE.get(instrument, "weekly"),
    }
