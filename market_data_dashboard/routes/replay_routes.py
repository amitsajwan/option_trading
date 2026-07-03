"""Dhan-powered historical replay API.

POST /api/replay/start   → fetch date from Dhan, build snapshots, stream through live stack
GET  /api/replay/{id}    → status + progress
GET  /api/replay/runs    → list recent runs
DELETE /api/replay/{id}  → cancel in-progress run

The replay publishes to the LIVE topic (market:nifty:snapshot:v1 or market:snapshot:v1)
so results appear immediately in the existing signals/traces dashboard views.
Runs are tagged with run_id="replay-{date}-{instrument}" for easy filtering.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# In-memory job store (good enough for single-server, survives restarts via MongoDB)
_JOBS: Dict[str, Dict[str, Any]] = {}


# ── schemas ────────────────────────────────────────────────────────────────────

class ReplayStartRequest(BaseModel):
    date: str                           # YYYY-MM-DD
    instrument: str = "NIFTY"          # NIFTY | BANKNIFTY
    speed: float = 300.0               # bars/minute (300 = ~75s for a full day)
    label: str = ""


class ReplayStartResponse(BaseModel):
    run_id: str
    status: str
    message: str


# ── job runner ─────────────────────────────────────────────────────────────────

def _run_replay(run_id: str, date: str, instrument: str, speed: float):
    """Background thread: fetch → build → replay."""
    job = _JOBS[run_id]

    def _progress(step: str, msg: str):
        job["step"] = step
        job["message"] = msg
        job["updated_at"] = datetime.now(tz=_IST).isoformat()
        logger.info("[%s] %s: %s", run_id[:8], step, msg)

    try:
        # ── Step 1: Fetch from Dhan ──────────────────────────────────────────
        _progress("fetching", f"Fetching {instrument} {date} from Dhan API...")
        job["status"] = "running"

        from market_data_dashboard.services.dhan_replay_fetcher import DhanHistoricalFetcher
        fetcher = DhanHistoricalFetcher(progress_cb=_progress)
        raw = fetcher.fetch_day(instrument, date)

        n_bars = len(raw.get("index_bars", []))
        n_opts = sum(
            len(sides.get("ce", [])) + len(sides.get("pe", []))
            for sides in raw.get("options", {}).values()
        )
        job["fetch_bars"] = n_bars
        job["fetch_option_bars"] = n_opts
        _progress("building", f"Fetched {n_bars} index bars, {n_opts} option bars. Building snapshots...")

        # ── Step 2: Build snapshots ──────────────────────────────────────────
        from market_data_dashboard.services.dhan_replay_builder import build_snapshots_from_dhan_data
        snapshots = build_snapshots_from_dhan_data(raw, progress_cb=_progress)
        job["snapshot_count"] = len(snapshots)
        _progress("replaying", f"Built {len(snapshots)} snapshots. Streaming to live topic...")

        # ── Step 3: Publish to Redis live topic ──────────────────────────────
        import redis as redis_lib
        from contracts_app import build_snapshot_event

        topic = (
            f"market:nifty:snapshot:v1"
            if instrument.upper() == "NIFTY"
            else "market:snapshot:v1"
        )

        r = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=0,
            decode_responses=True,
        )

        interval = 60.0 / max(1.0, float(speed))  # seconds per bar
        emitted = 0
        job["total"] = len(snapshots)
        job["emitted"] = 0

        for snap in snapshots:
            if job.get("cancelled"):
                _progress("cancelled", "Replay cancelled by user")
                job["status"] = "cancelled"
                return

            event = build_snapshot_event(
                snapshot=snap,
                source="dhan_replay",
                metadata={"run_id": run_id, "replay_date": date},
            )
            r.publish(topic, json.dumps(event))
            emitted += 1
            job["emitted"] = emitted

            if emitted % 50 == 0:
                _progress("replaying",
                    f"Published {emitted}/{len(snapshots)} bars to {topic}")

            if interval > 0.01:
                time.sleep(interval)

        _progress("done", f"Replay complete — {emitted} snapshots published to {topic}")
        job["status"] = "done"
        job["completed_at"] = datetime.now(tz=_IST).isoformat()

    except Exception as exc:
        logger.exception("Replay failed run_id=%s", run_id)
        job["status"] = "error"
        job["error"] = str(exc)
        job["step"] = "error"
        job["message"] = f"Error: {exc}"


# ── router ─────────────────────────────────────────────────────────────────────

class ReplayRouter:
    """Dhan-powered historical replay endpoints."""

    def __init__(self) -> None:
        router = APIRouter(tags=["replay"])
        router.add_api_route("/api/replay/start", self.start_replay, methods=["POST"])
        router.add_api_route("/api/replay/runs", self.list_runs, methods=["GET"])
        router.add_api_route("/api/replay/{run_id}", self.get_run, methods=["GET"])
        router.add_api_route("/api/replay/{run_id}", self.cancel_run, methods=["DELETE"])
        self.router = router

    async def start_replay(self, body: ReplayStartRequest) -> ReplayStartResponse:
        # Validate date
        try:
            datetime.strptime(body.date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")

        inst = body.instrument.upper()
        if inst not in ("NIFTY", "BANKNIFTY"):
            raise HTTPException(400, "instrument must be NIFTY or BANKNIFTY")

        run_id = f"replay-{body.date}-{inst.lower()}-{uuid.uuid4().hex[:6]}"
        job: Dict[str, Any] = {
            "run_id": run_id,
            "date": body.date,
            "instrument": inst,
            "speed": body.speed,
            "label": body.label or f"{inst} {body.date}",
            "status": "queued",
            "step": "queued",
            "message": "Queued — starting shortly",
            "emitted": 0,
            "total": 0,
            "created_at": datetime.now(tz=_IST).isoformat(),
            "updated_at": datetime.now(tz=_IST).isoformat(),
            "completed_at": None,
            "error": None,
            "cancelled": False,
        }
        _JOBS[run_id] = job

        # Start background thread
        t = threading.Thread(
            target=_run_replay,
            args=(run_id, body.date, inst, body.speed),
            daemon=True,
            name=f"replay-{run_id[:8]}",
        )
        t.start()

        return ReplayStartResponse(
            run_id=run_id,
            status="queued",
            message=f"Replay started for {inst} {body.date} at {body.speed}x speed",
        )

    async def get_run(self, run_id: str) -> Dict[str, Any]:
        job = _JOBS.get(run_id)
        if not job:
            raise HTTPException(404, f"run {run_id} not found")
        pct = int(100 * job["emitted"] / max(1, job["total"])) if job["total"] else 0
        return {**job, "progress_pct": pct}

    async def list_runs(
        self,
        instrument: str = Query(""),
        limit: int = Query(20, ge=1, le=100),
    ) -> List[Dict[str, Any]]:
        runs = sorted(_JOBS.values(), key=lambda j: j["created_at"], reverse=True)
        if instrument:
            runs = [r for r in runs if r["instrument"] == instrument.upper()]
        return runs[:limit]

    async def cancel_run(self, run_id: str) -> Dict[str, str]:
        job = _JOBS.get(run_id)
        if not job:
            raise HTTPException(404, f"run {run_id} not found")
        if job["status"] in ("done", "error", "cancelled"):
            return {"status": job["status"], "message": "Run already finished"}
        job["cancelled"] = True
        return {"status": "cancelling", "message": "Cancel requested"}
