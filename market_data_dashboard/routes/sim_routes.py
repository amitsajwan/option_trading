from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import redis
from fastapi import APIRouter, HTTPException, Query
from pymongo import MongoClient

from contracts_app import SimManifest, compute_config_hash, resolve_git_commit, resolve_namespace

from market_data_dashboard._namespace import (
    BASE_DECISION_TRACES,
    BASE_POSITIONS,
    BASE_SIGNALS,
    BASE_SNAPSHOTS,
    BASE_VOTES,
)

from .schemas.sim import SimRunCreateRequest, SimRunCreateResponse, SimRunSummary

ALLOWED_ENV_OVERRIDE_KEYS = {
    "STRATEGY_PROFILE_ID",
    "STRATEGY_ENGINE",
    "STRATEGY_MIN_CONFIDENCE",
    "ENTRY_TIME_WINDOWS",
    "ENTRY_REGIME_ALLOWED_TAGS",
    "ENTRY_REGIME_TAGGER",
    "ML_ENTRY_DIRECTION_MODE",
    "ML_ENTRY_PE_ONLY",
    "ML_ENTRY_CE_ONLY",
    "ML_ENTRY_BLOCK_PE",
    "ML_ENTRY_BLOCK_CE",
    "DIRECTION_ML_MODEL_PATH",
    "DIRECTION_ML_WEIGHT",
    "DIRECTION_ML_FILTER_MIN_PROB",
    "ENTRY_ML_MODEL_PATH",
    "ENTRY_ML_MIN_PROB",
    "ML_PURE_RUN_ID",
    "ML_PURE_MODEL_GROUP",
    "ML_PURE_MODEL_PACKAGE",
    "OPTION_PNL_MODEL_BUNDLE",
    "BRAIN_ENABLED",
    "BRAIN_CONSENSUS_MIN_AGREEING",
    "STRATEGY_IV_EXTREME_PERCENTILE",
    "DEPTH_FEED_ENABLED",
    "DEPTH_STALE_SEC",
    "STRATEGY_STRIKE_SELECTION_POLICY",
    "STRATEGY_STRIKE_MAX_OTM_STEPS",
    "STRATEGY_STRIKE_MIN_OI",
    "STRATEGY_STRIKE_MIN_VOLUME",
    "STRATEGY_STRIKE_LIQUIDITY_WEIGHT",
    "STRATEGY_STRIKE_AFFORDABILITY_WEIGHT",
    "STRATEGY_STRIKE_DISTANCE_PENALTY",
    "STRATEGY_SMART_STRIKE_ENABLED",
    "SMART_STRIKE_IV_REJECT_PCTILE",
    "SMART_STRIKE_OTM_CONFIDENCE",
    "SMART_STRIKE_OTM_IV_CEIL",
    "SMART_STRIKE_OTM2_ENABLED",
    "SMART_STRIKE_OTM2_CONFIDENCE",
    "SMART_STRIKE_OTM2_IV_CEIL",
    "SMART_STRIKE_OTM2_REGIMES",
    "SMART_STRIKE_OTM2_MAX_BAR_HOUR",
}

SIM_EVENT_START = "sim_run_start"
SIM_EVENT_CANCEL = "sim_run_cancel"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _new_run_id() -> str:
    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())  # type: ignore[attr-defined]
    return str(uuid.uuid4())


def _default_db():
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        client = MongoClient(uri)
    else:
        client = MongoClient(
            host=str(os.getenv("MONGO_HOST") or "localhost"),
            port=int(os.getenv("MONGO_PORT") or "27017"),
        )
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    return client[db_name]


def _runs_collection_name() -> str:
    return str(os.getenv("MONGO_COLL_STRATEGY_EVAL_RUNS") or "strategy_eval_runs")


def _command_channel() -> str:
    return str(os.getenv("SIM_COMMAND_TOPIC") or "sim:run:command").strip() or "sim:run:command"


def _default_redis() -> redis.Redis:
    return redis.Redis(
        host=str(os.getenv("REDIS_HOST") or "localhost"),
        port=int(os.getenv("REDIS_PORT") or "6379"),
        db=int(os.getenv("REDIS_DB") or "0"),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _default_publish_command(payload: dict[str, Any]) -> None:
    client = _default_redis()
    client.publish(_command_channel(), json.dumps(payload, ensure_ascii=False, default=str))


class DashboardSimRouter:
    def __init__(
        self,
        *,
        get_db: Callable[[], Any] = _default_db,
        publish_command: Callable[[dict[str, Any]], None] = _default_publish_command,
        run_dir_root: Optional[Path] = None,
    ) -> None:
        self._get_db = get_db
        self._publish_command = publish_command
        self._run_dir_root = Path(run_dir_root) if run_dir_root is not None else None

        router = APIRouter(tags=["sim"])
        router.add_api_route("/api/sim/runs", self.create_run, methods=["POST"], response_model=SimRunCreateResponse)
        router.add_api_route("/api/sim/runs", self.list_runs, methods=["GET"])
        router.add_api_route("/api/sim/runs/{run_id}", self.get_run, methods=["GET"])
        router.add_api_route("/api/sim/runs/{run_id}", self.cancel_run, methods=["DELETE"])
        self.router = router

    def _runs_coll(self):
        return self._get_db()[_runs_collection_name()]

    @staticmethod
    def _validate_env_overrides(env_overrides: Dict[str, str]) -> None:
        unknown = sorted([key for key in env_overrides.keys() if key not in ALLOWED_ENV_OVERRIDE_KEYS])
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown env_overrides keys: {', '.join(unknown)}")

    def create_run(self, body: SimRunCreateRequest) -> SimRunCreateResponse:
        self._validate_env_overrides(body.env_overrides)
        run_id = _new_run_id()
        ns = resolve_namespace("sim", run_id=run_id)
        run_dir = (self._run_dir_root / run_id) if self._run_dir_root is not None else ns.run_dir_for()
        if run_dir.exists():
            raise HTTPException(status_code=409, detail=f"run dir already exists: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=False)

        image_digest = str(os.getenv("STRATEGY_APP_SIM_IMAGE_DIGEST") or "pending")
        config_hash = compute_config_hash(
            env_overrides=body.env_overrides,
            image_digest=image_digest,
            speed=body.speed,
        )
        manifest_path = run_dir / "manifest.json"
        stream_name = ns.stream_for("snapshots")
        submitted_at = _now_iso()
        git_commit = resolve_git_commit()

        doc = {
            "run_id": run_id,
            "kind": "sim",
            "status": "queued",
            "terminal_status": "running",
            "source_date": body.source_date,
            "source_coll": body.source_coll,
            "label": body.label,
            "stream_name": stream_name,
            "manifest_path": str(manifest_path),
            "run_dir": str(run_dir),
            "speed": float(body.speed),
            "env_overrides": dict(body.env_overrides),
            "config_hash": config_hash,
            "git_commit": git_commit,
            "image_digest": image_digest,
            "submitted_at": submitted_at,
            "updated_at": submitted_at,
        }
        runs = self._runs_coll()
        runs.insert_one(doc)

        manifest = SimManifest(
            run_id=run_id,
            kind="sim",
            source_date=body.source_date,
            source_coll=body.source_coll,
            label=body.label,
            git_commit=git_commit,
            config_hash=config_hash,
            env_overrides=dict(body.env_overrides),
            image_digest=image_digest,
            speed=float(body.speed),
            created_at=submitted_at,
            terminal_status="running",
        )
        manifest.write_to(run_dir)

        self._publish_command(
            {
                "event_type": SIM_EVENT_START,
                "event_version": "1.0",
                "run_id": run_id,
                "source_date": body.source_date,
                "source_coll": body.source_coll,
                "label": body.label,
                "speed": float(body.speed),
                "env_overrides": dict(body.env_overrides),
                "submitted_at": submitted_at,
            }
        )

        return SimRunCreateResponse(
            run_id=run_id,
            manifest_path=str(manifest_path),
            stream_name=stream_name,
            dashboard_url=f"/app?mode=replay&kind=sim&run_id={run_id}&date={body.source_date}",
        )

    def list_runs(
        self,
        date: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"kind": "sim"}
        if date:
            query["source_date"] = str(date)
        rows = list(self._runs_coll().find(query, {"_id": 0}).sort("submitted_at", -1).limit(int(limit)))
        return {"rows": [SimRunSummary(**self._normalize_row(row)).model_dump() for row in rows]}

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        run_id = str(row.get("run_id") or "")
        run_dir = Path(str(row.get("run_dir") or ""))
        result_path = run_dir / "result.json"
        cancellation_path = run_dir / "cancellation.json"
        if result_path.exists():
            row["terminal_status"] = "completed"
            row["status"] = "completed"
        elif cancellation_path.exists():
            row["terminal_status"] = "cancelled"
            row["status"] = "cancelled"
        if run_id:
            ns = resolve_namespace("sim", run_id=run_id)
            db = self._get_db()
            counts: dict[str, int] = {}
            for label, base in (
                ("snapshots", BASE_SNAPSHOTS),
                ("votes", BASE_VOTES),
                ("signals", BASE_SIGNALS),
                ("positions", BASE_POSITIONS),
                ("decision_traces", BASE_DECISION_TRACES),
            ):
                coll = ns.collection_for(base)
                try:
                    counts[label] = int(db[coll].count_documents({"run_id": run_id}))
                except Exception:
                    counts[label] = 0
            row.setdefault("metadata", {})
            row["metadata"]["collection_counts"] = counts
        return row

    def get_run(self, run_id: str) -> dict[str, Any]:
        rid = str(run_id or "").strip()
        row = self._runs_coll().find_one({"run_id": rid}, {"_id": 0})
        if not row:
            raise HTTPException(status_code=404, detail=f"run_id not found: {rid}")
        normalized = self._normalize_row(dict(row))
        if normalized.get("status") != row.get("status"):
            self._runs_coll().update_one(
                {"run_id": rid},
                {
                    "$set": {
                        "status": normalized.get("status"),
                        "terminal_status": normalized.get("terminal_status"),
                        "updated_at": _now_iso(),
                    }
                },
            )
        return SimRunSummary(**normalized).model_dump()

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        rid = str(run_id or "").strip()
        row = self._runs_coll().find_one({"run_id": rid}, {"_id": 0})
        if not row:
            raise HTTPException(status_code=404, detail=f"run_id not found: {rid}")

        self._runs_coll().update_one(
            {"run_id": rid},
            {"$set": {"status": "cancel_requested", "updated_at": _now_iso()}},
        )
        self._publish_command(
            {
                "event_type": SIM_EVENT_CANCEL,
                "event_version": "1.0",
                "run_id": rid,
            }
        )
        return {"run_id": rid, "status": "cancel_requested"}


__all__ = [
    "DashboardSimRouter",
    "ALLOWED_ENV_OVERRIDE_KEYS",
    "SIM_EVENT_START",
    "SIM_EVENT_CANCEL",
]
