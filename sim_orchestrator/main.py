"""SIM replay orchestrator.

Listens on a Redis pub/sub command channel for sim run start/cancel requests,
spawns the publisher subprocess and per-run ``strategy_app_sim`` consumer,
and updates ``strategy_eval_runs`` when runs complete.

Dashboard ``POST /api/sim/runs`` only enqueues work here; this service owns
Docker/subprocess execution.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import redis
from pymongo import MongoClient

from contracts_app import resolve_namespace

logger = logging.getLogger(__name__)

EVENT_START = "sim_run_start"
EVENT_CANCEL = "sim_run_cancel"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _command_channel() -> str:
    return str(os.getenv("SIM_COMMAND_TOPIC") or "sim:run:command").strip() or "sim:run:command"


def _runs_collection_name() -> str:
    return str(os.getenv("MONGO_COLL_STRATEGY_EVAL_RUNS") or "strategy_eval_runs")


def _repo_root() -> Path:
    return Path(str(os.getenv("REPO_ROOT") or "/opt/option_trading")).resolve()


def _redis_client() -> redis.Redis:
    return redis.Redis(
        host=str(os.getenv("REDIS_HOST") or "localhost"),
        port=int(os.getenv("REDIS_PORT") or "6379"),
        db=int(os.getenv("REDIS_DB") or "0"),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _mongo_runs_collection():
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        client = MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000)
    else:
        client = MongoClient(
            host=str(os.getenv("MONGO_HOST") or "localhost"),
            port=int(os.getenv("MONGO_PORT") or "27017"),
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=5000,
        )
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    return client, client[db_name][_runs_collection_name()]


def _python_bin() -> str:
    venv = _repo_root() / ".venv" / "bin" / "python"
    if venv.is_file():
        return str(venv)
    return str(os.getenv("SIM_PYTHON_BIN") or "python3")


def _spawn_env() -> dict[str, str]:
    env = dict(os.environ)
    root = str(_repo_root())
    env["PYTHONPATH"] = os.pathsep.join([root, env.get("PYTHONPATH", ""), "/app"]).strip(os.pathsep)
    env.setdefault("REDIS_HOST", str(os.getenv("REDIS_HOST") or "localhost"))
    env.setdefault("MONGO_HOST", str(os.getenv("MONGO_HOST") or "localhost"))
    env.setdefault("MONGO_DB", str(os.getenv("MONGO_DB") or "trading_ai"))
    return env


def spawn_publisher(
    *,
    run_id: str,
    source_date: str,
    source_coll: str,
    label: str,
    speed: float,
    image_digest: str,
    env_overrides: Mapping[str, str],
) -> int:
    args = [
        _python_bin(),
        "-m",
        "ops.sim.run_sim_publisher",
        "--run-id",
        run_id,
        "--source-date",
        source_date,
        "--source-coll",
        source_coll,
        "--speed",
        str(speed),
        "--label",
        label,
        "--image-digest",
        image_digest,
        "--env-overrides-json",
        json.dumps(dict(env_overrides)),
    ]
    proc = subprocess.Popen(args, env=_spawn_env(), cwd=str(_repo_root()))
    return int(proc.pid)


def _compose_files() -> list[str]:
    root = _repo_root()
    files = [root / "docker-compose.yml"]
    gcp = root / "docker-compose.gcp.yml"
    if gcp.is_file():
        files.append(gcp)
    return [str(p) for p in files]


def spawn_consumer(run_id: str) -> str:
    root = _repo_root()
    env_file = root / ".env.compose"
    cmd: list[str] = ["docker", "compose"]
    for f in _compose_files():
        cmd.extend(["-f", f])
    if env_file.is_file():
        cmd.extend(["--env-file", str(env_file)])
    cmd.extend(
        [
            "run",
            "--rm",
            "-d",
            "-e",
            f"SIM_RUN_ID={run_id}",
            "strategy_app_sim",
        ]
    )
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=str(root))
    return str(result.stdout or "").strip()


def spawn_persistence(run_id: str) -> str:
    root = _repo_root()
    env_file = root / ".env.compose"
    cmd: list[str] = ["docker", "compose"]
    for f in _compose_files():
        cmd.extend(["-f", f])
    if env_file.is_file():
        cmd.extend(["--env-file", str(env_file)])
    cmd.extend(
        [
            "run",
            "--rm",
            "-d",
            "-e",
            f"SIM_RUN_ID={run_id}",
            "strategy_persistence_sim",
        ]
    )
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=str(root))
    return str(result.stdout or "").strip()


def stop_consumer(container_id: str) -> None:
    text = str(container_id or "").strip()
    if not text:
        return
    subprocess.run(["docker", "stop", text], check=False, capture_output=True, text=True)


def resolve_image_digest() -> str:
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
            cwd=str(_repo_root()),
        )
        text = str(out.stdout or "").strip()
        return text or "unknown"
    except Exception:
        return "unknown"


def _seal_run_dir(run_dir: Path) -> None:
    try:
        for path in [run_dir, *run_dir.rglob("*")]:
            try:
                mode = path.stat().st_mode
                path.chmod(mode & ~0o222)
            except Exception:
                continue
    except Exception:
        return


def _watch_terminal_status(
    coll: Any,
    run_id: str,
    run_dir: Path,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
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
            _seal_run_dir(run_dir)
            coll.update_one(
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
            logger.info("sim run terminal run_id=%s status=%s", run_id, terminal_status)
            return
        sleep_fn(1.0)
    logger.warning("sim run watch timeout run_id=%s", run_id)


def _handle_start(coll: Any, payload: Mapping[str, Any]) -> None:
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        logger.warning("sim start command missing run_id")
        return

    row = coll.find_one({"run_id": run_id}, {"_id": 0})
    if not row:
        logger.warning("sim start command run_id not in registry: %s", run_id)
        return

    source_date = str(payload.get("source_date") or row.get("source_date") or "")
    source_coll = str(payload.get("source_coll") or row.get("source_coll") or "phase1_market_snapshots")
    label = str(payload.get("label") or row.get("label") or "")
    speed = float(payload.get("speed") or row.get("speed") or 30.0)
    env_overrides = payload.get("env_overrides") or row.get("env_overrides") or {}
    if not isinstance(env_overrides, dict):
        env_overrides = {}

    image_digest = str(row.get("image_digest") or "").strip() or resolve_image_digest()
    if image_digest == "unknown" or not row.get("image_digest"):
        coll.update_one(
            {"run_id": run_id},
            {"$set": {"image_digest": image_digest, "updated_at": _now_iso()}},
        )

    try:
        publisher_pid = spawn_publisher(
            run_id=run_id,
            source_date=source_date,
            source_coll=source_coll,
            label=label,
            speed=speed,
            image_digest=image_digest,
            env_overrides=env_overrides,
        )
        persistence_container_id = spawn_persistence(run_id)
        container_id = spawn_consumer(run_id)
    except Exception as exc:
        logger.exception("sim run spawn failed run_id=%s: %s", run_id, exc)
        coll.update_one(
            {"run_id": run_id},
            {
                "$set": {
                    "status": "failed",
                    "terminal_status": "failed",
                    "error": str(exc),
                    "updated_at": _now_iso(),
                }
            },
        )
        return

    coll.update_one(
        {"run_id": run_id},
        {
            "$set": {
                "status": "running",
                "terminal_status": "running",
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                "publisher_pid": int(publisher_pid),
                "consumer_container_id": container_id,
                "persistence_container_id": persistence_container_id,
            }
        },
    )

    ns = resolve_namespace("sim", run_id=run_id)
    run_dir = ns.run_dir_for()
    watcher = threading.Thread(
        target=_watch_terminal_status,
        args=(coll, run_id, run_dir),
        daemon=True,
        name=f"sim-run-watch-{run_id[:8]}",
    )
    watcher.start()
    logger.info(
        "sim run started run_id=%s publisher_pid=%s container_id=%s",
        run_id,
        publisher_pid,
        container_id,
    )


def _handle_cancel(coll: Any, payload: Mapping[str, Any]) -> None:
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        return
    row = coll.find_one({"run_id": run_id}, {"_id": 0})
    if not row:
        logger.warning("sim cancel run_id not found: %s", run_id)
        return

    pid = row.get("publisher_pid")
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    container_id = str(row.get("consumer_container_id") or "").strip()
    if container_id:
        stop_consumer(container_id)
    persistence_container_id = str(row.get("persistence_container_id") or "").strip()
    if persistence_container_id:
        stop_consumer(persistence_container_id)

    coll.update_one(
        {"run_id": run_id},
        {
            "$set": {
                "status": "cancelled",
                "terminal_status": "cancelled",
                "updated_at": _now_iso(),
            }
        },
    )
    logger.info("sim run cancelled run_id=%s", run_id)


def process_command(coll: Any, payload: Mapping[str, Any]) -> None:
    event = str(payload.get("event_type") or "").strip()
    if event == EVENT_START:
        _handle_start(coll, payload)
    elif event == EVENT_CANCEL:
        _handle_cancel(coll, payload)
    else:
        logger.warning("unknown sim command event_type=%r", event)


def run_loop() -> int:
    redis_client = _redis_client()
    mongo_client, runs_coll = _mongo_runs_collection()
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    channel = _command_channel()
    pubsub.subscribe(channel)
    logger.info("sim_orchestrator subscribed topic=%s repo_root=%s", channel, _repo_root())
    try:
        while True:
            msg = pubsub.get_message(timeout=1.0)
            if not msg:
                time.sleep(0.01)
                continue
            data = msg.get("data")
            if not isinstance(data, str):
                continue
            try:
                payload = json.loads(data)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            process_command(runs_coll, payload)
    except KeyboardInterrupt:
        logger.info("sim_orchestrator interrupted")
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
        try:
            mongo_client.close()
        except Exception:
            pass
    return 0


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SIM replay orchestrator")
    parser.parse_args(list(argv) if argv is not None else None)
    return run_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    raise SystemExit(run_cli())
