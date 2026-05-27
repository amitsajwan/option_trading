from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from fastapi import APIRouter, HTTPException, Query

from contracts_app import SimManifest, compute_config_hash, resolve_git_commit, resolve_namespace

from .schemas.sim import SimRunCreateRequest, SimRunCreateResponse, SimRunSummary

ALLOWED_ENV_OVERRIDE_KEYS = {
    "STRATEGY_PROFILE_ID",
    "STRATEGY_ENGINE",
    "ENTRY_TIME_WINDOWS",
    "ENTRY_REGIME_ALLOWED_TAGS",
    "ENTRY_REGIME_TAGGER",
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
}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _new_run_id() -> str:
    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())  # type: ignore[attr-defined]
    return str(uuid.uuid4())


def _default_db():
    from pymongo import MongoClient

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


def _default_publisher_spawn(args: list[str], env: dict[str, str]) -> int:
    proc = subprocess.Popen(args, env=env)
    return int(proc.pid)


def _default_consumer_spawn(run_id: str) -> str:
    cmd = [
        "docker",
        "compose",
        "--env-file",
        ".env.compose",
        "run",
        "--rm",
        "-d",
        "-e",
        f"SIM_RUN_ID={run_id}",
        "strategy_app_sim",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return str(result.stdout or "").strip()


def _default_consumer_stop(container_id: str) -> None:
    if not str(container_id or "").strip():
        return
    subprocess.run(["docker", "stop", str(container_id)], check=False, capture_output=True, text=True)


def _default_image_digest() -> str:
    image = str(os.getenv("STRATEGY_APP_SIM_IMAGE") or "option_trading-strategy_app_sim").strip()
    if not image:
        return "unknown"
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        )
        text = str(out.stdout or "").strip()
        return text or "unknown"
    except Exception:
        return "unknown"


class DashboardSimRouter:
    def __init__(
        self,
        *,
        get_db: Callable[[], Any] = _default_db,
        spawn_publisher: Callable[[list[str], dict[str, str]], int] = _default_publisher_spawn,
        spawn_consumer: Callable[[str], str] = _default_consumer_spawn,
        stop_consumer: Callable[[str], None] = _default_consumer_stop,
        get_image_digest: Callable[[], str] = _default_image_digest,
        run_dir_root: Optional[Path] = None,
    ) -> None:
        self._get_db = get_db
        self._spawn_publisher = spawn_publisher
        self._spawn_consumer = spawn_consumer
        self._stop_consumer = stop_consumer
        self._get_image_digest = get_image_digest
        self._run_dir_root = Path(run_dir_root) if run_dir_root is not None else None

        router = APIRouter(tags=["sim"])
        router.add_api_route("/api/sim/runs", self.create_run, methods=["POST"], response_model=SimRunCreateResponse)
        router.add_api_route("/api/sim/runs", self.list_runs, methods=["GET"])
        router.add_api_route("/api/sim/runs/{run_id}", self.get_run, methods=["GET"])
        router.add_api_route("/api/sim/runs/{run_id}", self.cancel_run, methods=["DELETE"])
        self.router = router

    def _runs_coll(self):
        return self._get_db()[_runs_collection_name()]

    def _seal_run_dir(self, run_dir: Path) -> None:
        try:
            targets: Iterable[Path] = [run_dir] + list(run_dir.rglob("*"))
            for path in targets:
                try:
                    mode = path.stat().st_mode
                    path.chmod(mode & ~0o222)
                except Exception:
                    continue
        except Exception:
            return

    def _watch_terminal_status(self, run_id: str, run_dir: Path) -> None:
        runs = self._runs_coll()
        result_path = run_dir / "result.json"
        cancellation_path = run_dir / "cancellation.json"
        deadline = time.time() + (60.0 * 60.0 * 6.0)
        while time.time() < deadline:
            terminal_status: Optional[str] = None
            if result_path.exists():
                terminal_status = "completed"
            elif cancellation_path.exists():
                terminal_status = "cancelled"
            if terminal_status:
                self._seal_run_dir(run_dir)
                runs.update_one(
                    {"run_id": run_id},
                    {
                        "$set": {
                            "status": terminal_status,
                            "terminal_status": terminal_status,
                            "completed_at": _now_iso(),
                            "updated_at": _now_iso(),
                        }
                    },
                )
                return
            time.sleep(1.0)

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

        image_digest = self._get_image_digest()
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

        publisher_args = [
            "python",
            "-m",
            "ops.sim.run_sim_publisher",
            "--run-id",
            run_id,
            "--source-date",
            body.source_date,
            "--source-coll",
            body.source_coll,
            "--speed",
            str(body.speed),
            "--label",
            body.label,
            "--image-digest",
            image_digest,
            "--env-overrides-json",
            json.dumps(body.env_overrides),
        ]
        child_env = dict(os.environ)
        publisher_pid = self._spawn_publisher(publisher_args, child_env)
        container_id = self._spawn_consumer(run_id)

        runs.update_one(
            {"run_id": run_id},
            {
                "$set": {
                    "status": "running",
                    "started_at": _now_iso(),
                    "updated_at": _now_iso(),
                    "publisher_pid": int(publisher_pid),
                    "consumer_container_id": container_id,
                }
            },
        )
        watcher = threading.Thread(
            target=self._watch_terminal_status,
            args=(run_id, run_dir),
            daemon=True,
            name=f"sim-run-watch-{run_id[:8]}",
        )
        watcher.start()

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
            for kind in ["snapshots", "votes", "signals", "positions", "decision_traces"]:
                coll = ns.collection_for(kind)
                try:
                    counts[kind] = int(db[coll].count_documents({"run_id": run_id}))
                except Exception:
                    counts[kind] = 0
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
                {"$set": {"status": normalized.get("status"), "terminal_status": normalized.get("terminal_status"), "updated_at": _now_iso()}},
            )
        return SimRunSummary(**normalized).model_dump()

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        rid = str(run_id or "").strip()
        row = self._runs_coll().find_one({"run_id": rid}, {"_id": 0})
        if not row:
            raise HTTPException(status_code=404, detail=f"run_id not found: {rid}")

        pid = row.get("publisher_pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

        container_id = str(row.get("consumer_container_id") or "").strip()
        if container_id:
            self._stop_consumer(container_id)

        self._runs_coll().update_one(
            {"run_id": rid},
            {"$set": {"status": "cancelled", "terminal_status": "cancelled", "updated_at": _now_iso()}},
        )
        return {"run_id": rid, "status": "cancelled"}


__all__ = ["DashboardSimRouter", "ALLOWED_ENV_OVERRIDE_KEYS"]

