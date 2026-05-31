"""Pipeline decision event API routes.

Endpoints:
    GET  /api/pipeline/latest              — last 50 trace summaries (all stages collapsed)
    GET  /api/pipeline/trace/{trace_id}    — full 7-stage chain for one trace
    GET  /api/regime/timeline              — regime sequence for a run/session
    GET  /api/depth/current                — latest depth event (CE/PE bid strength)
    GET  /api/plugins/registry             — active plugins per stage, deduplicated
    GET  /api/streams/health               — consumer lag per stream via Redis XINFO GROUPS
    WS   /ws/pipeline                      — push pipeline_update events when new docs arrive

All REST endpoints are read-only.  No state mutation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

import redis
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

try:
    from pymongo import ASCENDING, DESCENDING
except ImportError:
    ASCENDING  = 1   # type: ignore[assignment]
    DESCENDING = -1  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_COLLECTION = "pipeline_decision_events"
_STAGE_ORDER = ["regime", "entry", "direction", "depth", "strike", "risk", "execution"]

_REGIME_COLORS = {
    "TRENDING":    "#22c55e",
    "SIDEWAYS":    "#71717a",
    "CHOP":        "#eab308",
    "BREAKOUT":    "#06b6d4",
    "PANIC":       "#f97316",
    "DEAD_MARKET": "#3f3f46",
    "HIGH_VOL":    "#f59e0b",
    "AVOID":       "#ef4444",
    "EXPIRY":      "#a855f7",
    "PRE_EXPIRY":  "#c084fc",
}


# ---------------------------------------------------------------------------
# Shared DB / Redis helpers (lazy, cached per process)
# ---------------------------------------------------------------------------

_db_cache: dict[str, Any] = {}


def _get_collection():
    if "coll" in _db_cache:
        return _db_cache["coll"]
    try:
        from pymongo import ASCENDING, DESCENDING, MongoClient
        uri  = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
        name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
        sel  = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS") or "3000")
        if uri:
            client = MongoClient(uri, serverSelectionTimeoutMS=sel)
        else:
            client = MongoClient(
                host=str(os.getenv("MONGO_HOST") or "localhost"),
                port=int(os.getenv("MONGO_PORT") or "27017"),
                serverSelectionTimeoutMS=sel,
            )
        _db_cache["coll"] = client[name][_COLLECTION]
        return _db_cache["coll"]
    except Exception as exc:
        logger.warning("pipeline_routes: mongo unavailable: %s", exc)
        return None


def _redis_client():
    if "redis" in _db_cache:
        return _db_cache["redis"]
    try:
        from contracts_app import redis_connection_kwargs
        _db_cache["redis"] = redis.Redis(
            **redis_connection_kwargs(decode_responses=True, for_pubsub=False)
        )
        return _db_cache["redis"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _sanitize(obj: Any) -> Any:
    """Recursively replace NaN/Inf floats with None so json.dumps never crashes."""
    if isinstance(obj, float):
        import math
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _stage_sort_key(stage: str) -> int:
    try:
        return _STAGE_ORDER.index(stage)
    except ValueError:
        return 99


def _collapse_trace(docs: list[dict]) -> dict:
    """Collapse 7 stage docs into one summary row."""
    docs_sorted = sorted(docs, key=lambda d: _stage_sort_key(d.get("stage", "")))
    first = docs_sorted[0] if docs_sorted else {}
    summary: dict[str, Any] = {
        "trace_id":    first.get("trace_id", ""),
        "run_id":      first.get("run_id", ""),
        "parity_mode": first.get("parity_mode", ""),
        "timestamp":   first.get("timestamp", ""),
        "stages":      {},
    }
    for doc in docs_sorted:
        stage = doc.get("stage", "unknown")
        summary["stages"][stage] = {
            "outcome":    doc.get("outcome", ""),
            "confidence": doc.get("confidence"),
            "plugin_id":  doc.get("plugin_id", ""),
        }
    regime_stage = summary["stages"].get("regime", {})
    exec_stage   = summary["stages"].get("execution", {})
    summary["regime"]       = regime_stage.get("outcome", "").split(" ")[0]
    summary["signal_type"]  = exec_stage.get("outcome", "SKIP")
    summary["regime_color"] = _REGIME_COLORS.get(summary["regime"], "#71717a")
    return summary


def _dt_to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class PipelineRouter:
    def __init__(self) -> None:
        router = APIRouter(tags=["pipeline"])
        router.add_api_route("/api/pipeline/latest",           self.get_latest,         methods=["GET"])
        router.add_api_route("/api/pipeline/trace/{trace_id}", self.get_trace,           methods=["GET"])
        router.add_api_route("/api/regime/timeline",           self.get_regime_timeline, methods=["GET"])
        router.add_api_route("/api/depth/current",             self.get_depth_current,   methods=["GET"])
        router.add_api_route("/api/plugins/registry",          self.get_plugins_registry,methods=["GET"])
        router.add_api_route("/api/streams/health",            self.get_streams_health,  methods=["GET"])
        router.add_api_websocket_route("/ws/pipeline",         self.websocket_pipeline)
        self.router = router

    # ── GET /api/pipeline/latest ───────────────────────────────────────────

    async def get_latest(self, limit: int = Query(default=50, ge=1, le=200)):
        """Return the last `limit` trace summaries, one row per trace_id."""
        coll = _get_collection()
        if coll is None:
            return {"traces": [], "error": "mongodb unavailable"}

        try:
            raw = list(coll.find(
                {},
                {"trace_id": 1, "run_id": 1, "stage": 1, "outcome": 1,
                 "confidence": 1, "plugin_id": 1, "parity_mode": 1, "timestamp": 1, "_id": 0},
                sort=[("_received_at", DESCENDING)],
                limit=limit * 8,  # fetch enough to cover limit full traces
            ))
        except Exception as exc:
            logger.warning("pipeline latest query failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        by_trace: dict[str, list[dict]] = defaultdict(list)
        for doc in raw:
            tid = doc.get("trace_id", "")
            if tid:
                by_trace[tid].append(doc)

        traces = [_collapse_trace(docs) for docs in list(by_trace.values())[:limit]]
        return _sanitize({"traces": traces, "total": len(traces)})

    # ── GET /api/pipeline/trace/{trace_id} ────────────────────────────────

    async def get_trace(self, trace_id: str):
        """Return all stage events for one trace, sorted by stage order."""
        coll = _get_collection()
        if coll is None:
            return {"trace_id": trace_id, "stages": [], "error": "mongodb unavailable"}

        try:
            raw = list(coll.find(
                {"trace_id": trace_id},
                {"_id": 0, "payload": 1, "stage": 1, "outcome": 1,
                 "confidence": 1, "plugin_id": 1, "plugin_version": 1,
                 "event_id": 1, "parent_event_id": 1, "timestamp": 1,
                 "run_id": 1, "parity_mode": 1},
                sort=[("_received_at", ASCENDING)],
            ))
        except Exception as exc:
            logger.warning("pipeline trace query failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        if not raw:
            raise HTTPException(status_code=404, detail=f"trace_id {trace_id!r} not found")

        stages = sorted(raw, key=lambda d: _stage_sort_key(d.get("stage", "")))
        return _sanitize({"trace_id": trace_id, "stages": stages, "stage_count": len(stages)})

    # ── GET /api/regime/timeline ───────────────────────────────────────────

    async def get_regime_timeline(
        self,
        run_id: str = Query(default=""),
        limit:  int = Query(default=200, ge=1, le=1000),
    ):
        """Return regime sequence for a run, ordered chronologically."""
        coll = _get_collection()
        if coll is None:
            return {"regimes": [], "error": "mongodb unavailable"}

        try:
            query: dict[str, Any] = {"stage": "regime"}
            if run_id:
                query["run_id"] = run_id
            raw = list(coll.find(
                query,
                {"_id": 0, "trace_id": 1, "run_id": 1, "timestamp": 1,
                 "outcome": 1, "confidence": 1, "payload": 1, "parity_mode": 1},
                sort=[("_received_at", ASCENDING)],
                limit=limit,
            ))
        except Exception as exc:
            logger.warning("regime timeline query failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        regimes = []
        for i, doc in enumerate(raw):
            p = doc.get("payload") or {}
            regime_name = str(p.get("regime") or doc.get("outcome") or "").split(" ")[0]
            entry: dict[str, Any] = {
                "trace_id":  doc.get("trace_id", ""),
                "run_id":    doc.get("run_id", ""),
                "timestamp": doc.get("timestamp", ""),
                "regime":    regime_name,
                "confidence": doc.get("confidence"),
                "evidence":  p.get("evidence", {}),
                "color":     _REGIME_COLORS.get(regime_name, "#71717a"),
                "seq":       i,
            }
            # Compute duration by diffing against next entry's timestamp
            regimes.append(entry)

        return _sanitize({"regimes": regimes, "total": len(regimes), "run_id": run_id})

    # ── GET /api/depth/current ─────────────────────────────────────────────

    async def get_depth_current(self, run_id: str = Query(default="")):
        """Return the most recent depth decision event."""
        coll = _get_collection()
        if coll is None:
            return {"depth_available": False, "error": "mongodb unavailable"}

        try:
            query: dict[str, Any] = {"stage": "depth"}
            if run_id:
                query["run_id"] = run_id
            doc = coll.find_one(
                query,
                {"_id": 0, "payload": 1, "timestamp": 1, "trace_id": 1,
                 "run_id": 1, "confidence": 1, "outcome": 1, "_received_at": 1},
                sort=[("_received_at", DESCENDING)],
            )
        except Exception as exc:
            logger.warning("depth current query failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        if not doc:
            return {"depth_available": False, "run_id": run_id or None}

        p = doc.get("payload") or {}
        return {
            "trace_id":       doc.get("trace_id", ""),
            "run_id":         doc.get("run_id", ""),
            "timestamp":      doc.get("timestamp", ""),
            "updated_at":     _dt_to_str(doc.get("_received_at")),
            "ce_bid_strength": p.get("ce_bid_strength"),
            "pe_bid_strength": p.get("pe_bid_strength"),
            "spread_pct":     p.get("spread_pct"),
            "depth_aligned":  bool(p.get("depth_aligned", False)),
            "depth_available": bool(p.get("depth_available", False)),
            "direction":      str(p.get("direction") or ""),
            "confidence":     doc.get("confidence"),
            "proceed":        bool(p.get("proceed", True)),
            "skip_reason":    p.get("skip_reason"),
        }

    # ── GET /api/plugins/registry ──────────────────────────────────────────

    async def get_plugins_registry(self, run_id: str = Query(default="")):
        """Return active plugins per stage, deduplicated by (stage, plugin_id, plugin_version)."""
        coll = _get_collection()
        if coll is None:
            return {"plugins": [], "error": "mongodb unavailable"}

        try:
            query: dict[str, Any] = {}
            if run_id:
                query["run_id"] = run_id
            raw = list(coll.find(
                query,
                {"_id": 0, "stage": 1, "plugin_id": 1, "plugin_version": 1,
                 "parity_mode": 1, "run_id": 1, "_received_at": 1},
                sort=[("_received_at", DESCENDING)],
                limit=500,
            ))
        except Exception as exc:
            logger.warning("plugins registry query failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        # Keep only the first (most recent) occurrence of each (stage, plugin_id, plugin_version)
        seen: dict[tuple, dict] = {}
        for doc in raw:
            key = (doc.get("stage", ""), doc.get("plugin_id", ""), doc.get("plugin_version", ""))
            if key not in seen:
                seen[key] = doc

        plugins = []
        for (stage, pid, pver), doc in seen.items():
            if not pid:
                continue
            plugins.append({
                "stage":          stage,
                "plugin_id":      pid,
                "plugin_version": pver,
                "parity_mode":    doc.get("parity_mode", ""),
                "run_id":         doc.get("run_id", ""),
                "last_seen":      _dt_to_str(doc.get("_received_at")),
            })

        plugins.sort(key=lambda p: _stage_sort_key(p["stage"]))
        return {"plugins": plugins, "total": len(plugins)}

    # ── GET /api/streams/health ────────────────────────────────────────────

    async def get_streams_health(self):
        """Return consumer lag per pipeline stream via Redis XINFO GROUPS."""
        rc = _redis_client()
        if rc is None:
            return {"streams": [], "error": "redis unavailable"}

        from contracts_app import resolve_namespace

        run_id = str(os.getenv("SIM_RUN_ID") or "").strip()
        if not run_id:
            return {"streams": [], "note": "SIM_RUN_ID not set — only SIM streams supported"}

        ns = resolve_namespace("sim", run_id=run_id)
        slugs = [
            "regime_decisions", "entry_decisions", "direction_decisions",
            "depth_decisions",  "strike_decisions", "risk_decisions", "execution_events",
        ]

        results = []
        now_ts = time.time()
        for slug in slugs:
            stream = ns.stream_for(slug)
            entry: dict[str, Any] = {
                "stream": slug, "stream_key": stream,
                "lag": None, "last_event_age_sec": None, "status": "unknown",
            }
            try:
                groups = rc.xinfo_groups(stream)
                if groups:
                    total_lag = sum(int(g.get("lag") or g.get("pending") or 0) for g in groups)
                    entry["lag"] = total_lag
                last = rc.xrevrange(stream, count=1)
                if last:
                    msg_id = last[0][0]
                    ms = int(str(msg_id).split("-")[0])
                    entry["last_event_age_sec"] = round(now_ts - ms / 1000.0, 1)
                entry["status"] = (
                    "stale" if (entry.get("last_event_age_sec") or 9999) > 30
                    else "warn" if (entry.get("lag") or 0) > 50
                    else "ok"
                )
            except Exception as exc:
                entry["status"] = "error"
                entry["error"]  = str(exc)
            results.append(entry)

        return {"streams": results}

    # ── WebSocket /ws/pipeline ─────────────────────────────────────────────

    async def websocket_pipeline(self, ws: WebSocket) -> None:
        """Push pipeline_update events whenever new docs arrive in MongoDB."""
        await ws.accept()
        last_seen: Optional[datetime] = None

        async def _poll() -> None:
            nonlocal last_seen
            while True:
                coll = _get_collection()
                if coll is not None:
                    try:
                        query: dict[str, Any] = {}
                        if last_seen is not None:
                            query["_received_at"] = {"$gt": last_seen}
                        new_docs = list(coll.find(
                            query,
                            {"trace_id": 1, "run_id": 1, "stage": 1, "outcome": 1,
                             "confidence": 1, "plugin_id": 1, "parity_mode": 1,
                             "timestamp": 1, "_received_at": 1, "_id": 0},
                            sort=[("_received_at", DESCENDING)],
                            limit=200,
                        ))
                        if new_docs:
                            candidate = max(
                                (d["_received_at"] for d in new_docs if d.get("_received_at")),
                                default=None,
                            )
                            if candidate and (last_seen is None or candidate > last_seen):
                                last_seen = candidate
                                by_trace: dict[str, list] = defaultdict(list)
                                for doc in new_docs:
                                    if doc.get("trace_id"):
                                        by_trace[doc["trace_id"]].append(doc)
                                traces = [_collapse_trace(docs) for docs in by_trace.values()]
                                await ws.send_json({"type": "pipeline_update", "traces": traces[:50]})
                    except Exception as exc:
                        logger.debug("pipeline WS poll error: %s", exc)
                await asyncio.sleep(2.0)

        task = asyncio.create_task(_poll())
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    import json
                    msg = json.loads(raw)
                    if msg.get("action") == "ping":
                        await ws.send_json({"type": "pong"})
                except Exception:
                    pass
        except (WebSocketDisconnect, Exception) as exc:
            logger.debug("pipeline WS disconnected: %s", exc)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
