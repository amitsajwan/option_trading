"""Decision trace streaming — REST + WebSocket.

GET  /api/signals        — paginated decision traces from MongoDB
WS   /ws/signals         — real-time stream via Redis decision_trace channel

Each event tells the UI: at bar T, what was the entry_prob, direction,
which gates passed/failed, and whether a trade was taken or blocked.

This makes the per-bar decision pipeline visible without SSH or log tailing.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

try:
    from .._namespace import BASE_DECISION_TRACES, collection_for
    from ..real_source import make_mongo_db
except ImportError:
    from market_data_dashboard._namespace import BASE_DECISION_TRACES, collection_for  # type: ignore
    from market_data_dashboard.real_source import make_mongo_db  # type: ignore

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)
_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> str:
    return datetime.now(tz=_IST).isoformat()


def _redis_url() -> str:
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    return f"redis://{host}:{port}/0"


def _decision_trace_topic(instrument: Optional[str] = None) -> str:
    """Live decision-trace pubsub topic, scoped to the requested instrument.

    An explicit env override (STRATEGY_DECISION_TRACE_TOPIC) wins; otherwise the
    default is scoped by instrument so a NIFTY client subscribes to
    market:nifty:strategy:decision_trace:v1.
    """
    explicit = str(os.getenv("STRATEGY_DECISION_TRACE_TOPIC") or "").strip()
    if explicit:
        return explicit
    try:
        from contracts_app.topics import scope_topic_for_instrument
        return scope_topic_for_instrument("market:strategy:decision_trace:v1", instrument)
    except Exception:
        return "market:strategy:decision_trace:v1"


def _shape_trace(doc: dict[str, Any]) -> dict[str, Any]:
    """Project a MongoDB decision_trace document into the API contract shape."""
    payload = doc.get("payload") or {}
    candidates = payload.get("candidates") or []

    # Find the selected candidate (if any)
    selected = next((c for c in candidates if c.get("selected")), None)

    # Extract gate results from the selected candidate's flow_gates
    gates: dict[str, bool] = {}
    if selected:
        for gate in selected.get("flow_gates") or []:
            gid = gate.get("gate_id") or gate.get("id") or ""
            passed = gate.get("passed", gate.get("status") == "passed")
            if gid:
                gates[gid] = bool(passed)

    # Entry prob: from payload or top-level
    entry_prob = (
        payload.get("ml_entry_prob")
        or doc.get("payload", {}).get("entry_quality_grade_metrics", {}).get("ml_entry_prob")
        or doc.get("ml_entry_prob")
    )

    # Direction confidence: from the selected candidate
    dir_conf = None
    if selected:
        dir_conf = selected.get("confidence")

    return {
        "ts": doc.get("timestamp") or doc.get("market_time_ist"),
        "trade_date": doc.get("trade_date_ist"),
        "instrument": doc.get("instrument") or "BANKNIFTY",
        "entry_prob": _safe_float(entry_prob),
        "regime": (payload.get("regime_context") or {}).get("regime") or payload.get("regime"),
        "direction": doc.get("selected_direction"),
        "direction_conf": _safe_float(dir_conf),
        "outcome": doc.get("final_outcome"),
        "block_reason": doc.get("primary_blocker_gate"),
        "gates": gates,
        "trade_id": doc.get("position_id") or None,
        "run_id": doc.get("run_id"),
    }


class SignalsRouter:
    """REST + WebSocket endpoints for decision trace streaming."""

    def __init__(self) -> None:
        router = APIRouter(tags=["signals"])
        router.add_api_route("/api/signals", self.get_signals, methods=["GET"])
        router.add_api_websocket_route("/ws/signals", self.ws_signals)
        self.router = router

    # ── REST ─────────────────────────────────────────────────────────────────

    async def get_signals(
        self,
        instrument: str = Query("BANKNIFTY"),
        date: str = Query(..., description="YYYY-MM-DD"),
        outcome: str = Query("", description="blocked | hold | entry_taken | exit_taken | manage_only"),
        limit: int = Query(500, ge=1, le=2000),
        offset: int = Query(0, ge=0),
        kind: str = Query("live", description="live | oos | sim"),
    ) -> dict[str, Any]:
        try:
            db = make_mongo_db()
        except Exception as exc:
            return {"error": f"mongo unavailable: {exc}"}

        coll = db[collection_for(BASE_DECISION_TRACES, kind=kind, instrument=instrument)]
        query: dict[str, Any] = {"trade_date_ist": date}
        if outcome:
            query["final_outcome"] = outcome

        total = coll.count_documents(query)
        docs = list(
            coll.find(query, {"_id": 0})
            .sort("timestamp", 1)
            .skip(offset)
            .limit(limit)
        )

        signals = [_shape_trace(d) for d in docs]

        # Summary stats
        all_outcomes = [d.get("final_outcome") for d in docs]
        summary: dict[str, Any] = {"total_filtered": total}
        for outcome_val in ("entry_taken", "exit_taken", "hold", "blocked", "manage_only"):
            summary[outcome_val] = all_outcomes.count(outcome_val)

        # Gate failure breakdown
        block_gates: dict[str, int] = {}
        for d in docs:
            if d.get("final_outcome") == "blocked":
                gate = d.get("primary_blocker_gate") or "unknown"
                block_gates[gate] = block_gates.get(gate, 0) + 1
        summary["blocked_by_gate"] = dict(sorted(block_gates.items(), key=lambda x: -x[1]))

        return {"signals": signals, "summary": summary, "offset": offset, "limit": limit}

    # ── WebSocket ─────────────────────────────────────────────────────────────

    async def ws_signals(
        self,
        websocket: WebSocket,
        instrument: str = Query("BANKNIFTY"),
    ) -> None:
        """Stream real-time decision traces from Redis pub/sub.

        Client receives one JSON message per bar the engine evaluates.
        Same shape as /api/signals items.
        """
        await websocket.accept()
        topic = _decision_trace_topic(instrument)
        redis_url = _redis_url()

        try:
            r = aioredis.from_url(redis_url, decode_responses=True)
            pubsub = r.pubsub()
            await pubsub.subscribe(topic)
            logger.info("ws_signals: subscribed to %s for %s", topic, instrument)

            await websocket.send_json({
                "type": "connected",
                "topic": topic,
                "instrument": instrument,
                "ts": _now_ist(),
            })

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

                event = _shape_trace_from_redis(payload, instrument)
                await websocket.send_json({"type": "signal", **event})

        except WebSocketDisconnect:
            logger.info("ws_signals: client disconnected")
        except Exception as exc:
            logger.warning("ws_signals error: %s", exc)
            try:
                await websocket.send_json({"type": "error", "detail": str(exc)})
            except Exception:
                pass
        finally:
            try:
                await pubsub.unsubscribe(topic)
                await r.aclose()
            except Exception:
                pass
            try:
                await websocket.close()
            except Exception:
                pass


def _shape_trace_from_redis(payload: dict[str, Any], instrument: str) -> dict[str, Any]:
    """Project a raw Redis decision-trace payload into the API shape.

    The Redis payload is the full trace dict published by DeterministicRuleEngine.
    """
    candidates = payload.get("candidates") or []
    selected = next((c for c in candidates if c.get("selected")), None)

    gates: dict[str, bool] = {}
    if selected:
        for gate in selected.get("flow_gates") or []:
            gid = gate.get("gate_id") or gate.get("id") or ""
            passed = gate.get("passed", gate.get("status") == "passed")
            if gid:
                gates[gid] = bool(passed)

    regime_ctx = payload.get("regime_context") or {}
    summary = payload.get("summary_metrics") or {}

    entry_prob = summary.get("ml_entry_prob") or (
        (selected or {}).get("metrics", {}) or {}
    ).get("ml_entry_prob")

    return {
        "ts": payload.get("timestamp"),
        "instrument": instrument,
        "entry_prob": _safe_float(entry_prob),
        "regime": regime_ctx.get("regime"),
        "direction": payload.get("selected_direction") or (selected or {}).get("direction"),
        "direction_conf": _safe_float((selected or {}).get("confidence")),
        "outcome": payload.get("final_outcome"),
        "block_reason": payload.get("primary_blocker_gate"),
        "gates": gates,
        "trade_id": None,
    }


def _safe_float(v: Any) -> Optional[float]:
    try:
        return round(float(v), 4) if v is not None else None
    except (ValueError, TypeError):
        return None
