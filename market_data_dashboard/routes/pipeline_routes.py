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

_COLLECTION      = "pipeline_decision_events"
_SIM_COLLECTION  = "strategy_decision_traces_sim"
# Live + replay decision traces (one doc per evaluated bar, same nested
# payload.trace schema as the sim collection). This is where real trading
# writes — 298k+ docs vs the sparse pipeline_decision_events.
_LIVE_COLLECTION = "strategy_decision_traces"
_STAGE_ORDER    = ["regime", "entry", "direction", "depth", "strike", "risk", "execution"]

# Maps primary_blocker_gate prefixes → which stage blocked
_BLOCKER_TO_STAGE: dict[str, str] = {
    "avoid_veto":          "regime",
    "regime":              "regime",
    "brain_gate":          "entry",
    "entry_phase":         "entry",
    "direction_consensus": "direction",
    "direction":           "direction",
    "depth":               "depth",
    "strike":              "strike",
    "risk_pause":          "risk",
    "risk":                "risk",
}

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


def _get_sim_collection():
    if "sim_coll" in _db_cache:
        return _db_cache["sim_coll"]
    try:
        coll = _get_collection()
        if coll is None:
            return None
        db = coll.database
        _db_cache["sim_coll"] = db[_SIM_COLLECTION]
        return _db_cache["sim_coll"]
    except Exception as exc:
        logger.warning("pipeline_routes: sim collection unavailable: %s", exc)
        return None


def _get_live_collection():
    if "live_coll" in _db_cache:
        return _db_cache["live_coll"]
    try:
        coll = _get_collection()
        if coll is None:
            return None
        _db_cache["live_coll"] = coll.database[_LIVE_COLLECTION]
        return _db_cache["live_coll"]
    except Exception as exc:
        logger.warning("pipeline_routes: live collection unavailable: %s", exc)
        return None


def _get_depth_collection():
    if "depth_coll" in _db_cache:
        return _db_cache["depth_coll"]
    try:
        coll = _get_collection()
        if coll is None:
            return None
        db = coll.database
        _db_cache["depth_coll"] = db["market_depth_ticks"]
        return _db_cache["depth_coll"]
    except Exception as exc:
        logger.warning("pipeline_routes: depth collection unavailable: %s", exc)
        return None


def _bid_strength_from_tick(doc: dict | None) -> float | None:
    if not doc:
        return None
    total_bid = doc.get("total_bid_qty") or 0
    total_ask = doc.get("total_ask_qty") or 0
    total = total_bid + total_ask
    return round(total_bid / total, 3) if total > 0 else 0.5


def _read_live_depth_from_redis() -> dict | None:
    """Read CE/PE depth directly from the Redis keys written by depth_collector.

    Returns None if Redis is unavailable, keys are missing, or data is stale
    (> 120 s old). The caller falls through to the MongoDB path when None.
    """
    try:
        from contracts_app import get_redis_key, redis_connection_kwargs
        import json as _json
        import time as _time

        r = redis.Redis(**redis_connection_kwargs(decode_responses=True))
        raw_ce = r.get(get_redis_key("depth:atm_ce:latest"))
        raw_pe = r.get(get_redis_key("depth:atm_pe:latest"))
        if not raw_ce and not raw_pe:
            return None

        ce = _json.loads(raw_ce) if raw_ce else {}
        pe = _json.loads(raw_pe) if raw_pe else {}

        now_epoch = _time.time()
        ce_age = now_epoch - (ce.get("fetched_at_epoch") or 0)
        pe_age = now_epoch - (pe.get("fetched_at_epoch") or 0)
        if ce_age > 120 and pe_age > 120:
            return None

        ce_str = _bid_strength_from_tick(ce) if ce else None
        pe_str = _bid_strength_from_tick(pe) if pe else None
        # aligned = PE bid strength exceeds CE — indicates put-side is dominant
        aligned = bool((pe_str or 0) > (ce_str or 0))
        dominant = "PE" if aligned else "CE"
        ts = (ce or pe).get("fetched_at", "")
        return {
            "trace_id": "",
            "run_id": "",
            "timestamp": ts,
            "updated_at": ts,
            "ce_bid_strength": ce_str,
            "pe_bid_strength": pe_str,
            "ce_instrument": ce.get("instrument", ""),
            "pe_instrument": pe.get("instrument", ""),
            "spread_pct": ce.get("spread"),
            "depth_aligned": aligned,
            "depth_dominant": dominant,
            "depth_available": True,
            "direction": "",
            "confidence": None,
            "proceed": True,
            "skip_reason": None,
            "source": "redis_live",
        }
    except Exception as exc:
        logger.debug("live depth redis read failed: %s", exc)
        return None


def _blocker_stage(blocker: str) -> str:
    for prefix, stage in _BLOCKER_TO_STAGE.items():
        if blocker.startswith(prefix) or prefix in blocker:
            return stage
    return "risk"


def _adapt_sim_trace(doc: dict) -> dict:
    """Convert a strategy_decision_traces_sim summary doc to the pipeline frontend format."""
    final_outcome = doc.get("final_outcome") or "blocked"
    blocker       = doc.get("primary_blocker_gate") or ""

    signal_type = "SKIP" if final_outcome == "blocked" else final_outcome
    blocked_at  = _blocker_stage(blocker) if signal_type == "SKIP" else None

    stages: dict[str, Any] = {}
    for stage in _STAGE_ORDER:
        if blocked_at is None:
            outcome = signal_type if stage == "execution" else "PASS"
            stages[stage] = {"outcome": outcome, "confidence": None, "plugin_id": ""}
        else:
            if stage == blocked_at:
                stages[stage] = {"outcome": "SKIP", "confidence": None, "plugin_id": blocker}
                break
            stages[stage] = {"outcome": "PASS", "confidence": None, "plugin_id": ""}

    regime      = doc.get("selected_direction") or ""
    metric      = doc.get("summary_metrics") or {}
    return {
        "trace_id":    doc.get("trace_id", ""),
        "run_id":      doc.get("run_id", ""),
        "parity_mode": doc.get("engine_mode", ""),
        "timestamp":   doc.get("timestamp", ""),
        "stages":      stages,
        "regime":      regime,
        "signal_type": signal_type,
        "regime_color": _REGIME_COLORS.get(regime, "#71717a"),
        "blocker":     blocker,
        "summary_metrics": metric,
    }


_GATE_GROUP_TO_STAGE = {
    "regime":    "regime",
    "warmup":    "regime",
    "policy":    "entry",
    "entry":     "entry",
    "direction": "direction",
    "depth":     "depth",
    "strike":    "strike",
    "risk":      "risk",
    "execution": "execution",
}


def _stages_from_ordered_gates(gates: list[dict], final_outcome: str) -> list[dict]:
    """Convert ordered_gates array into the TraceTimeline stage list."""
    stages: list[dict] = []
    seen: set[str] = set()
    for g in gates:
        grp   = g.get("gate_group") or ""
        stage = _GATE_GROUP_TO_STAGE.get(grp, grp) or "entry"
        status = g.get("status") or "pass"
        outcome = "blocked" if status in ("blocked", "vetoed", "rejected") else "pass"
        met    = g.get("metrics") or {}
        conf   = met.get("confidence") or met.get("regime_confidence") or met.get("ml_confidence")
        entry: dict[str, Any] = {
            "stage":          stage if stage not in seen else f"{stage}:{g.get('gate_id','')}",
            "gate_id":        g.get("gate_id", ""),
            "outcome":        outcome,
            "confidence":     conf,
            "plugin_id":      g.get("gate_id", ""),
            "plugin_version": None,
            "payload": {
                "gate_id":     g.get("gate_id", ""),
                "gate_group":  grp,
                "reason_code": g.get("reason_code"),
                "message":     g.get("message"),
                **met,
            },
        }
        seen.add(stage)
        stages.append(entry)
        if outcome == "blocked":
            break

    if final_outcome not in ("blocked", "SKIP") and not any(s["stage"] == "execution" for s in stages):
        stages.append({
            "stage": "execution", "gate_id": "execution",
            "outcome": final_outcome, "confidence": None,
            "plugin_id": "", "plugin_version": None,
            "payload": {"final_outcome": final_outcome},
        })
    return stages


def _adapt_sim_trace_detail(doc: dict) -> dict:
    """Convert a sim summary doc to the TraceTimeline stages-array format."""
    final_outcome = doc.get("final_outcome") or "blocked"
    blocker       = doc.get("primary_blocker_gate") or ""
    metrics       = doc.get("summary_metrics") or {}
    payload_trace = (doc.get("payload") or {}).get("trace") or {}
    brain         = payload_trace.get("brain") or {}
    regime_ctx    = payload_trace.get("regime_context") or {}
    candidates    = payload_trace.get("candidates") or []
    flow_gates    = payload_trace.get("flow_gates") or []

    signal_type = "SKIP" if final_outcome == "blocked" else final_outcome

    # Prefer ordered_gates from the first candidate (most detailed)
    ordered_gates: list[dict] = []
    best_candidate: dict = {}
    for c in candidates:
        og = c.get("ordered_gates") or []
        if og and len(og) > len(ordered_gates):
            ordered_gates = og
            best_candidate = c
    if not ordered_gates:
        ordered_gates = flow_gates

    if ordered_gates:
        stages_list = _stages_from_ordered_gates(ordered_gates, final_outcome)
    else:
        # Fallback: synthetic from blocker
        blocked_at = _blocker_stage(blocker) if signal_type == "SKIP" else None
        stages_list = []
        for stage in _STAGE_ORDER:
            if blocked_at is None:
                outcome = signal_type if stage == "execution" else "pass"
                stages_list.append({"stage": stage, "gate_id": stage, "outcome": outcome,
                                     "confidence": None, "plugin_id": "", "plugin_version": None, "payload": {}})
            else:
                if stage == blocked_at:
                    stages_list.append({"stage": stage, "gate_id": stage, "outcome": "blocked",
                                        "confidence": None, "plugin_id": blocker, "plugin_version": None,
                                        "payload": {"skip_reason": blocker, **metrics}})
                    break
                stages_list.append({"stage": stage, "gate_id": stage, "outcome": "pass",
                                     "confidence": None, "plugin_id": "", "plugin_version": None, "payload": {}})

    return {
        "trace_id":    doc.get("trace_id", ""),
        "run_id":      doc.get("run_id", ""),
        "stage_count": len(stages_list),
        "stages":      stages_list,
        "source":      "sim",
        "timestamp":   doc.get("timestamp", ""),
        "primary_blocker_gate": blocker,
        "final_outcome":        final_outcome,
        "summary_metrics":      metrics,
        "brain": brain,
        "regime_context": regime_ctx,
        "candidates": [
            {
                "strategy_name": c.get("strategy_name", ""),
                "candidate_type": c.get("candidate_type", ""),
                "direction":      c.get("direction", ""),
                "confidence":     c.get("confidence"),
                "rank":           c.get("rank"),
                "selected":       c.get("selected", False),
                "terminal_status": c.get("terminal_status", ""),
                "terminal_gate_id": c.get("terminal_gate_id", ""),
                "metrics":        c.get("metrics") or {},
            }
            for c in candidates
        ],
    }


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

    async def get_latest(self, limit: int = Query(default=50, ge=1, le=200),
                         run_id: Optional[str] = Query(default=None),
                         date: Optional[str] = Query(default=None)):
        """Return the last `limit` trace summaries, one row per trace_id.

        `date=YYYY-MM-DD` (and/or no run_id) → read LIVE/replay traces from
        strategy_decision_traces (the collection real trading writes to). This is
        what lights up the Pipeline view for live + historical sessions.
        """
        # Live/replay by date → strategy_decision_traces (per-bar, same schema as sim)
        if date or (run_id and run_id.startswith("paper-")) or (run_id and run_id.startswith("capped")):
            live_coll = _get_live_collection()
            if live_coll is not None:
                try:
                    q: dict = {}
                    if date:
                        q["trade_date_ist"] = date
                    if run_id:
                        q["run_id"] = run_id
                    raw = list(live_coll.find(
                        q,
                        {"_id": 0, "trace_id": 1, "run_id": 1, "final_outcome": 1,
                         "primary_blocker_gate": 1, "engine_mode": 1, "timestamp": 1,
                         "market_time_ist": 1, "selected_direction": 1, "summary_metrics": 1,
                         "snapshot_id": 1},
                        sort=[("timestamp", DESCENDING)],
                        limit=limit,
                    ))
                    if raw:
                        traces = [_adapt_sim_trace(doc) for doc in raw]
                        return _sanitize({"traces": traces, "total": len(traces), "source": "live"})
                except Exception as exc:
                    logger.warning("live trace query failed, falling through: %s", exc)

        # Sim run_id → query the sim summary collection (different schema)
        if run_id:
            sim_coll = _get_sim_collection()
            if sim_coll is not None:
                try:
                    raw = list(sim_coll.find(
                        {"run_id": run_id},
                        {"_id": 0, "trace_id": 1, "run_id": 1, "final_outcome": 1,
                         "primary_blocker_gate": 1, "engine_mode": 1, "timestamp": 1,
                         "selected_direction": 1, "summary_metrics": 1},
                        sort=[("received_at_ttl", ASCENDING)],
                        limit=limit,
                    ))
                    if raw:
                        traces = [_adapt_sim_trace(doc) for doc in raw]
                        return _sanitize({"traces": traces, "total": len(traces), "source": "sim"})
                except Exception as exc:
                    logger.warning("sim collection query failed, falling through: %s", exc)

        coll = _get_collection()
        if coll is None:
            return {"traces": [], "error": "mongodb unavailable"}

        mongo_filter: dict = {}
        if run_id:
            mongo_filter["run_id"] = run_id

        try:
            raw = list(coll.find(
                mongo_filter,
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
        return _sanitize({"traces": traces, "total": len(traces), "source": "live"})

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
            # Primary source for live/replay: strategy_decision_traces (per-bar,
            # nested payload.trace — same shape the sim detail adapter handles).
            live_coll = _get_live_collection()
            if live_coll is not None:
                try:
                    live_doc = live_coll.find_one({"trace_id": trace_id}, {"_id": 0})
                    if live_doc:
                        return _sanitize(_adapt_sim_trace_detail(live_doc))
                except Exception as exc:
                    logger.warning("live trace lookup failed: %s", exc)
            # Fall back to sim summary collection
            sim_coll = _get_sim_collection()
            if sim_coll is not None:
                try:
                    sim_doc = sim_coll.find_one({"trace_id": trace_id}, {"_id": 0})
                    if sim_doc:
                        return _sanitize(_adapt_sim_trace_detail(sim_doc))
                except Exception as exc:
                    logger.warning("sim trace lookup failed: %s", exc)
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
        # Sim run_id → pull regime_context from strategy_decision_traces_sim
        if run_id:
            sim_coll = _get_sim_collection()
            if sim_coll is not None:
                try:
                    raw = list(sim_coll.find(
                        {"run_id": run_id},
                        {"_id": 0, "trace_id": 1, "run_id": 1, "timestamp": 1,
                         "payload": 1},
                        sort=[("received_at_ttl", ASCENDING)],
                        limit=limit,
                    ))
                    if raw:
                        regimes = []
                        for i, doc in enumerate(raw):
                            rc = (doc.get("payload") or {}).get("trace", {}).get("regime_context") or {}
                            regime_name = str(rc.get("regime") or "").split(" ")[0]
                            if not regime_name:
                                continue
                            regimes.append({
                                "trace_id":  doc.get("trace_id", ""),
                                "run_id":    doc.get("run_id", ""),
                                "timestamp": doc.get("timestamp", ""),
                                "regime":    regime_name,
                                "confidence": rc.get("confidence"),
                                "evidence":  rc.get("evidence", {}),
                                "reason":    rc.get("reason", ""),
                                "color":     _REGIME_COLORS.get(regime_name, "#71717a"),
                                "seq":       i,
                            })
                        return _sanitize({"regimes": regimes, "total": len(regimes),
                                          "run_id": run_id, "source": "sim"})
                except Exception as exc:
                    logger.warning("sim regime timeline failed, falling through: %s", exc)

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
            regimes.append(entry)

        return _sanitize({"regimes": regimes, "total": len(regimes), "run_id": run_id})

    # ── GET /api/depth/current ─────────────────────────────────────────────

    async def get_depth_current(self, run_id: str = Query(default="")):
        """Return the most recent depth decision event.

        For live mode (no run_id), reads directly from Redis where depth_collector
        writes fresh data every 5 s. Falls through to MongoDB for sim/historical runs.
        """
        if not run_id:
            live = _read_live_depth_from_redis()
            if live is not None:
                return live

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
            # Fall back to market_depth_ticks for the sim date
            depth_coll = _get_depth_collection()
            if depth_coll is not None:
                try:
                    # Try to resolve the sim date from run_id via sim collection
                    sim_date: str | None = None
                    if run_id:
                        sim_coll = _get_sim_collection()
                        if sim_coll is not None:
                            first = sim_coll.find_one({"run_id": run_id}, {"trade_date_ist": 1})
                            sim_date = (first or {}).get("trade_date_ist")
                    date_filter: dict = {}
                    if sim_date:
                        date_filter["trade_date_ist"] = sim_date
                    ce = depth_coll.find_one(
                        {**date_filter, "instrument": {"$regex": "CE$"}},
                        sort=[("fetched_at_epoch", DESCENDING)],
                    )
                    pe = depth_coll.find_one(
                        {**date_filter, "instrument": {"$regex": "PE$"}},
                        sort=[("fetched_at_epoch", DESCENDING)],
                    )
                    if ce or pe:
                        ce_str = _bid_strength_from_tick(ce)
                        pe_str = _bid_strength_from_tick(pe)
                        aligned = (ce_str or 0) > 0.55 or (pe_str or 0) > 0.55
                        tick = ce or pe
                        return {
                            "trace_id": "", "run_id": run_id or "",
                            "timestamp": str((tick or {}).get("fetched_at_ist", "")),
                            "updated_at": str((tick or {}).get("fetched_at_ist", "")),
                            "ce_bid_strength": ce_str,
                            "pe_bid_strength": pe_str,
                            "ce_instrument": (ce or {}).get("instrument", ""),
                            "pe_instrument": (pe or {}).get("instrument", ""),
                            "spread_pct": (ce or {}).get("spread"),
                            "depth_aligned": aligned,
                            "depth_available": True,
                            "direction": "",
                            "confidence": None,
                            "proceed": True,
                            "skip_reason": None,
                            "source": "market_depth_ticks",
                            "trade_date": sim_date or "",
                        }
                except Exception as exc:
                    logger.warning("depth tick fallback failed: %s", exc)
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

        # Use the actual stream key pattern published by strategy_app_sim
        sim_streams = [
            ("snapshots",       f"stream:snapshots:sim:{run_id}"),
            ("decision_trace",  f"stream:decision_trace:sim:{run_id}"),
            ("votes",           f"stream:votes:sim:{run_id}"),
            ("signals",         f"stream:signals:sim:{run_id}"),
            ("positions",       f"stream:positions:sim:{run_id}"),
        ]

        results = []
        now_ts = time.time()
        for slug, stream in sim_streams:
            entry: dict[str, Any] = {
                "stream": slug, "stream_key": stream,
                "lag": None, "last_event_age_sec": None, "status": "unknown",
            }
            try:
                length = rc.xlen(stream)
                entry["length"] = length
                last = rc.xrevrange(stream, count=1)
                if last:
                    msg_id = last[0][0]
                    ms = int(str(msg_id).split("-")[0])
                    entry["last_event_age_sec"] = round(now_ts - ms / 1000.0, 1)
                # Completed sim streams are stale by design — show ok if they have data
                entry["status"] = "ok" if length > 0 else "warn"
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
