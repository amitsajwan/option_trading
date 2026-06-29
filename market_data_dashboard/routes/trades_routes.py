"""GET /api/trades — enriched trade list with entry_prob, direction_conf, exit_reason.

Joins strategy_positions (POSITION_CLOSE events) with strategy_votes to
enrich each trade with the entry bar's ML probabilities.

Designed for the P&L timeline scatter plot: each trade is one point with
x=entry_time, y=pnl_pct, size=entry_prob, color=CE/PE.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Query

try:
    from .._namespace import BASE_POSITIONS, collection_for
    from ..real_source import make_mongo_db
except ImportError:
    from market_data_dashboard._namespace import BASE_POSITIONS, collection_for  # type: ignore
    from market_data_dashboard.real_source import make_mongo_db  # type: ignore

logger = logging.getLogger(__name__)
_IST = timezone(timedelta(hours=5, minutes=30))


def _win_loss(pnl: Optional[float]) -> Optional[str]:
    if pnl is None:
        return None
    return "win" if pnl > 0 else "loss"


def _shape_trade(pos: dict[str, Any]) -> dict[str, Any]:
    pnl = _safe_float(pos.get("actual_return_pct") or pos.get("pnl_pct"))
    entry_time = pos.get("entry_time") or pos.get("market_time_ist") or ""
    exit_reason = (
        pos.get("exit_reason")
        or (pos.get("reason") or {}).get("code")
        or (pos.get("reason") or {}).get("reason_code")
    )
    return {
        "trade_id": pos.get("position_id"),
        "instrument": pos.get("instrument") or "BANKNIFTY",
        "date": pos.get("trade_date_ist"),
        "entry_time": str(entry_time)[:8] if entry_time else None,  # HH:MM:SS
        "side": pos.get("direction"),
        "entry_prob": _safe_float(pos.get("ml_entry_prob")),
        "direction_conf": _safe_float(
            pos.get("ml_ce_prob") if pos.get("direction") == "CE"
            else pos.get("ml_pe_prob")
        ),
        "ml_ce_prob": _safe_float(pos.get("ml_ce_prob")),
        "ml_pe_prob": _safe_float(pos.get("ml_pe_prob")),
        "regime": pos.get("regime") or (pos.get("reason") or {}).get("regime"),
        "strike": pos.get("strike"),
        "entry_price": _safe_float(pos.get("entry_premium")),
        "exit_price": _safe_float(pos.get("exit_premium")),
        "pnl_pct": pnl,
        "outcome": pos.get("actual_outcome") or _win_loss(pnl),
        "exit_reason": exit_reason,
        "hold_minutes": pos.get("bars_held"),
        "mfe_pct": _safe_float(pos.get("mfe_pct")),
        "mae_pct": _safe_float(pos.get("mae_pct")),
        "mode": pos.get("engine_mode") or "live",
        "run_id": pos.get("run_id"),
    }


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(trades)
    if count == 0:
        return {"count": 0}
    pnls = [t["pnl_pct"] for t in trades if t["pnl_pct"] is not None]
    wins = [p for p in pnls if p > 0]
    probs = [t["entry_prob"] for t in trades if t["entry_prob"] is not None]
    confs = [t["direction_conf"] for t in trades if t["direction_conf"] is not None]
    return {
        "count": count,
        "win_count": len(wins),
        "win_rate": round(len(wins) / max(len(pnls), 1), 4),
        "avg_pnl_pct": round(sum(pnls) / max(len(pnls), 1), 4) if pnls else None,
        "sum_pnl_pct": round(sum(pnls), 4) if pnls else None,
        "avg_entry_prob": round(sum(probs) / max(len(probs), 1), 4) if probs else None,
        "avg_direction_conf": round(sum(confs) / max(len(confs), 1), 4) if confs else None,
        "ce_count": sum(1 for t in trades if t["side"] == "CE"),
        "pe_count": sum(1 for t in trades if t["side"] == "PE"),
    }


class TradesRouter:
    """GET /api/trades — enriched trade list."""

    def __init__(self) -> None:
        router = APIRouter(tags=["trades"])
        router.add_api_route("/api/trades", self.get_trades, methods=["GET"])
        router.add_api_route("/api/trades/{trade_id}", self.get_trade, methods=["GET"])
        self.router = router

    async def get_trades(
        self,
        instrument: str = Query("BANKNIFTY"),
        from_date: str = Query(..., alias="from", description="YYYY-MM-DD"),
        to_date: str = Query(..., alias="to", description="YYYY-MM-DD"),
        outcome: str = Query("", description="win | loss"),
        side: str = Query("", description="CE | PE"),
        kind: str = Query("live", description="live | oos | sim"),
        limit: int = Query(500, ge=1, le=2000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        try:
            db = make_mongo_db()
        except Exception as exc:
            return {"error": f"mongo unavailable: {exc}"}

        coll = db[collection_for(BASE_POSITIONS, kind=kind, instrument=instrument)]

        query: dict[str, Any] = {
            "event": "POSITION_CLOSE",
            "trade_date_ist": {"$gte": from_date, "$lte": to_date},
        }
        if side:
            query["direction"] = side.upper()

        docs = list(
            coll.find(query, {"_id": 0})
            .sort("entry_time", 1)
            .skip(offset)
            .limit(limit)
        )

        trades = [_shape_trade(d) for d in docs]

        # Filter by outcome after shaping (since actual_outcome may be derived)
        if outcome:
            trades = [t for t in trades if t.get("outcome") == outcome]

        return {
            "trades": trades,
            "summary": _summary(trades),
            "from": from_date,
            "to": to_date,
            "offset": offset,
            "limit": limit,
        }

    async def get_trade(
        self,
        trade_id: str,
        instrument: str = Query("BANKNIFTY"),
        kind: str = Query("live", description="live | oos | sim"),
    ) -> dict[str, Any]:
        try:
            db = make_mongo_db()
        except Exception as exc:
            return {"error": f"mongo unavailable: {exc}"}

        coll = db[collection_for(BASE_POSITIONS, kind=kind, instrument=instrument)]
        doc = coll.find_one(
            {"position_id": trade_id, "event": "POSITION_CLOSE"},
            {"_id": 0},
        )
        if not doc:
            # Try POSITION_OPEN (trade still open)
            doc = coll.find_one({"position_id": trade_id}, {"_id": 0})
        if not doc:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"trade {trade_id} not found")
        return _shape_trade(doc)


def _safe_float(v: Any) -> Optional[float]:
    try:
        return round(float(v), 4) if v is not None else None
    except (ValueError, TypeError):
        return None
