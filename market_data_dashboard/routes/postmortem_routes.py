"""Postmortem tab backend — trade analysis from stored MongoDB positions + traces.

Endpoints:
    GET /api/postmortem/positions?date=YYYY-MM-DD   list closed positions for a date
    GET /api/postmortem/position/{pos_id}            full detail: position + trace + autopsy
"""
from __future__ import annotations

import logging
import math
from datetime import date as date_type, datetime
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

_TAG_CODE_NAMES = {
    1: "COST_MISS",
    2: "EXIT_MISS",
    3: "DIRECTION_MISS",
    4: "ENTRY_MISS",
    5: "NOISE",
}

_TAG_EXPLANATIONS = {
    "COST_MISS": "Gross P&L was positive but transaction costs flipped it negative. The move was real but too small relative to cost.",
    "EXIT_MISS": "Had significant MFE (unrealized peak) but gave it back before exit. Held too long or stop was too loose.",
    "DIRECTION_MISS": "Entered the wrong side. The move happened but in the opposite direction.",
    "ENTRY_MISS": "Entered at a poor bar — low move potential. Entry model misfired or regime was wrong.",
    "NOISE": "Small random loss within expected variance. No clear structural cause.",
    "UNKNOWN": "Cannot classify — missing P&L or MFE data.",
}


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return None


def _reconstruct_autopsy(pos: Dict[str, Any]) -> Dict[str, Any]:
    """Try stored reflection_tag_code first, then reconstruct from P&L fields."""
    metrics = pos.get("decision_metrics") if isinstance(pos.get("decision_metrics"), dict) else {}
    tag_code = metrics.get("reflection_tag_code")
    source = "stored"
    if tag_code is not None:
        try:
            tag = _TAG_CODE_NAMES.get(int(float(tag_code)), "UNKNOWN")
            return {"tag": tag, "source": source, "explanation": _TAG_EXPLANATIONS.get(tag, "")}
        except Exception:
            pass

    # Reconstruct from trade fields
    source = "reconstructed"
    gross = _safe_float(pos.get("gross_pnl_pct")) or 0.0
    net = _safe_float(pos.get("net_pnl_pct")) or 0.0
    mfe = _safe_float(pos.get("mfe_pct")) or 0.0

    if net >= 0:
        tag = "NOISE"  # winner, no loss to classify
    elif gross > 0 and net < 0:
        tag = "COST_MISS"
    elif mfe > abs(net) * 1.5 and mfe > 0.5:
        tag = "EXIT_MISS"
    else:
        tag = "NOISE"

    return {"tag": tag, "source": source, "explanation": _TAG_EXPLANATIONS.get(tag, "")}


def _extract_direction_signals(pos: Dict[str, Any], trace: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pull direction signals from position top-level fields then trace raw_signals."""
    out: Dict[str, Any] = {}
    for field in ("ml_entry_prob", "ml_ce_prob", "ml_pe_prob", "ml_direction_up_prob",
                  "entry_prob", "vwap_side", "regime", "regime_reason"):
        val = pos.get(field)
        if val is not None:
            out[field] = val
    if trace and isinstance(trace.get("raw_signals"), dict):
        rs = trace["raw_signals"]
        for key in ("entry_prob", "ml_ce_prob", "ml_pe_prob", "vwap_side",
                    "_regime", "_regime_conf", "_regime_reason",
                    "direction_ce_prob", "direction_pe_prob"):
            if key in rs and key not in out:
                out[key] = rs[key]
    entry_diag = (trace or {}).get("entry_model") if trace else None
    if isinstance(entry_diag, dict):
        out["entry_diag"] = entry_diag
    return out


def _format_position(pos: Dict[str, Any], trace: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    autopsy = _reconstruct_autopsy(pos)
    signals = _extract_direction_signals(pos, trace)

    entry_time = pos.get("entry_market_time_ist") or pos.get("market_time_ist") or ""
    exit_time = pos.get("exit_market_time_ist") or ""
    bars_held = pos.get("bars_held")
    if bars_held is None and entry_time and exit_time:
        try:
            fmt = "%H:%M:%S"
            et = datetime.strptime(entry_time[:8], fmt)
            xt = datetime.strptime(exit_time[:8], fmt)
            bars_held = int(abs((xt - et).total_seconds()) // 60)
        except Exception:
            bars_held = None

    return {
        "position_id": pos.get("position_id", ""),
        "position_id_short": str(pos.get("position_id", ""))[:8],
        "trade_date": pos.get("trade_date_ist", ""),
        "entry_time": entry_time,
        "exit_time": exit_time,
        "bars_held": bars_held,
        "direction": pos.get("direction", ""),
        "strike": pos.get("strike") or pos.get("proposed_strike"),
        "entry_premium": _safe_float(pos.get("entry_premium")),
        "exit_premium": _safe_float(pos.get("exit_premium")),
        "gross_pnl_pct": _safe_float(pos.get("gross_pnl_pct")),
        "net_pnl_pct": _safe_float(pos.get("net_pnl_pct")),
        "mfe_pct": _safe_float(pos.get("mfe_pct")),
        "mae_pct": _safe_float(pos.get("mae_pct")),
        "exit_reason": pos.get("exit_reason", ""),
        "autopsy": autopsy,
        "signals": signals,
        "regime": pos.get("regime") or (trace or {}).get("regime"),
        "snapshot_id": pos.get("snapshot_id", ""),
    }


class DashboardPostmortemRouter:
    def __init__(self, *, get_db: Callable[[], Any]) -> None:
        self._get_db = get_db
        router = APIRouter(tags=["postmortem"])
        router.add_api_route("/api/postmortem/positions", self.list_positions, methods=["GET"])
        router.add_api_route("/api/postmortem/position/{pos_id}", self.get_position, methods=["GET"])
        self.router = router

    def _db(self) -> Any:
        db = self._get_db()
        if db is None:
            raise HTTPException(status_code=503, detail="MongoDB unavailable")
        return db

    async def list_positions(
        self,
        date: str = Query(default="", description="YYYY-MM-DD — defaults to today"),
        kind: str = Query(default="live", description="live | sim"),
    ) -> List[Dict[str, Any]]:
        from market_data_dashboard._namespace import BASE_POSITIONS, collection_for
        if not date:
            date = date_type.today().isoformat()
        try:
            db = self._db()
            coll = db[collection_for(BASE_POSITIONS, kind=kind)]
            docs = list(coll.find(
                {"event": "POSITION_CLOSE", "trade_date_ist": date},
                {"_id": 0},
                sort=[("market_time_ist", 1)],
            ))
            seen: set[str] = set()
            result = []
            for doc in docs:
                pid = doc.get("position_id", "")
                if pid in seen:
                    continue
                seen.add(pid)
                result.append(_format_position(doc))
            return result
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("postmortem: list_positions date=%s", date)
            raise HTTPException(status_code=500, detail=str(exc))

    async def get_position(
        self,
        pos_id: str,
        kind: str = Query(default="live"),
    ) -> Dict[str, Any]:
        from market_data_dashboard._namespace import (
            BASE_DECISION_TRACES, BASE_POSITIONS, collection_for,
        )
        try:
            db = self._db()
            pos_coll = db[collection_for(BASE_POSITIONS, kind=kind)]
            trace_coll = db[collection_for(BASE_DECISION_TRACES, kind=kind)]

            # POSITION_CLOSE has the final P&L/MFE/exit fields
            pos_doc = pos_coll.find_one(
                {"position_id": pos_id, "event": "POSITION_CLOSE"},
                {"_id": 0},
            )
            if pos_doc is None:
                # Fallback: any event for this position
                pos_doc = pos_coll.find_one({"position_id": pos_id}, {"_id": 0})
            if pos_doc is None:
                raise HTTPException(status_code=404, detail=f"Position {pos_id} not found")

            # Find POSITION_OPEN to get exact entry time/snapshot_id
            open_doc = pos_coll.find_one(
                {"position_id": pos_id, "event": "POSITION_OPEN"},
                {"_id": 0},
            )
            if open_doc:
                pos_doc["entry_market_time_ist"] = open_doc.get("market_time_ist")
                pos_doc["snapshot_id"] = pos_doc.get("snapshot_id") or open_doc.get("snapshot_id")

            # Find decision trace at entry bar (snapshot_id match or position_id)
            snap_id = pos_doc.get("snapshot_id", "")
            trace_doc = None
            if snap_id:
                trace_doc = trace_coll.find_one({"snapshot_id": snap_id}, {"_id": 0})
            if trace_doc is None:
                trace_doc = trace_coll.find_one({"position_id": pos_id}, {"_id": 0})

            # All manage events (for timeline)
            manage_docs = list(pos_coll.find(
                {"position_id": pos_id, "event": "POSITION_MANAGE"},
                {"_id": 0, "market_time_ist": 1, "unrealized_pct": 1, "exit_reason": 1},
                sort=[("market_time_ist", 1)],
            ))

            result = _format_position(pos_doc, trace_doc)
            result["manage_events"] = manage_docs

            # Raw trace for advanced inspection
            if trace_doc:
                result["trace"] = {
                    "snapshot_id": trace_doc.get("snapshot_id"),
                    "outcome": trace_doc.get("outcome"),
                    "blocker": trace_doc.get("blocker"),
                    "regime": trace_doc.get("regime"),
                    "regime_conf": trace_doc.get("regime_conf"),
                    "flow_gates": trace_doc.get("flow_gates") or [],
                    "entry_model": trace_doc.get("entry_model") or {},
                    "raw_signals": trace_doc.get("raw_signals") or {},
                }

            return result
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("postmortem: get_position pos_id=%s", pos_id)
            raise HTTPException(status_code=500, detail=str(exc))
