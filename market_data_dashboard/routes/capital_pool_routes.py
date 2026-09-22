"""GET /api/capital-pool — shared capital pool status.

Born 2026-09-21 alongside contracts_app/capital_pool.py. Read-only
visibility into the shared ₹4L pool the 5 buyer instruments draw from when
CAPITAL_POOL_ENABLED=1: total/available/reserved plus a per-instrument
breakdown and cap headroom, modeled directly on instruments_routes.py's
fan-out-and-aggregate shape.

Data source (read-only): the single shared "capital_pool" Mongo document
(contracts_app/capital_pool.py -- deliberately not instrument-namespaced,
so this endpoint reads exactly one document regardless of instrument).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import APIRouter

try:
    from contracts_app import capital_pool, known_instruments
except Exception:  # pragma: no cover - defensive for partial installs
    capital_pool = None  # type: ignore[assignment]
    known_instruments = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _pool_enabled() -> bool:
    return str(os.getenv("CAPITAL_POOL_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _instrument_cap_rs(total: float) -> float:
    pct = float(os.getenv("CAPITAL_POOL_MAX_INSTRUMENT_SHARE") or "0.40")
    return total * pct


def build_capital_pool_status() -> dict[str, Any]:
    enabled = _pool_enabled()
    if not enabled or capital_pool is None:
        return {"enabled": False, "reason": "CAPITAL_POOL_ENABLED is off" if capital_pool is not None else "contracts_app.capital_pool unavailable"}

    try:
        doc = capital_pool.status()
    except Exception as exc:  # pragma: no cover - defensive, dashboard must not 500 on a pool read hiccup
        logger.warning("capital_pool_routes: status read failed: %s", exc)
        return {"enabled": True, "error": str(exc)}

    if doc is None:
        return {"enabled": True, "initialized": False}

    total = float(doc.get("total", 0.0))
    available = float(doc.get("available", 0.0))
    reserved_by_instrument: dict[str, float] = dict(doc.get("reserved_by_instrument", {}))
    reserved_total = sum(reserved_by_instrument.values())
    cap_rs = _instrument_cap_rs(total)

    instruments = known_instruments() if known_instruments is not None else sorted(reserved_by_instrument)
    per_instrument = {}
    for instrument in instruments:
        reserved = float(reserved_by_instrument.get(instrument, 0.0))
        per_instrument[instrument] = {
            "reserved": reserved,
            "cap": cap_rs,
            "pct_of_cap": (reserved / cap_rs) if cap_rs > 0 else 0.0,
        }

    return {
        "enabled": True,
        "initialized": True,
        "total": total,
        "available": available,
        "reserved_total": reserved_total,
        "instrument_cap_rs": cap_rs,
        "per_instrument": per_instrument,
        "updated_at": doc.get("updated_at"),
        "reconciliation": doc.get("reconciliation"),
    }


class CapitalPoolRouter:
    """GET /api/capital-pool — shared capital pool status."""

    def __init__(self) -> None:
        router = APIRouter(tags=["capital_pool"])
        router.add_api_route("/api/capital-pool", self.get_status, methods=["GET"])
        self.router = router

    async def get_status(self) -> dict[str, Any]:
        return build_capital_pool_status()
