from __future__ import annotations

import argparse
import json
import os
from typing import Any, Iterable, Optional

import redis

from contracts_app import find_matching_python_processes, isoformat_ist, redis_connection_kwargs
from .engines.runtime_artifacts import RuntimeArtifactStore, resolve_runtime_artifact_paths, summarize_runtime_artifacts


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def evaluate(*, topic: str, artifact_dir: Optional[str] = None, metrics_tail_lines: int = 10) -> tuple[dict[str, Any], int]:
    _ = topic
    process_matches = find_matching_python_processes(["strategy_app.main"])
    process_running = len(process_matches) > 0

    redis_ok = False
    redis_error = None
    try:
        client = redis.Redis(**redis_connection_kwargs(decode_responses=True, for_pubsub=False))
        redis_ok = bool(client.ping())
    except Exception as exc:
        redis_error = str(exc)

    session_enabled = _truthy(os.getenv("MARKET_SESSION_ENABLED", "0"))
    status = "healthy"
    code = 0
    if not redis_ok:
        status = "unhealthy"
        code = 2
    elif not process_running:
        status = "degraded" if session_enabled else "unhealthy"
        code = 1 if session_enabled else 2

    artifact_store = RuntimeArtifactStore(resolve_runtime_artifact_paths(artifact_dir).root if artifact_dir else None)
    runtime_config = artifact_store.read_config()
    runtime_state = artifact_store.read_state()
    runtime_metrics = artifact_store.read_metrics(tail_lines=max(1, int(metrics_tail_lines)))
    runtime_config_error = bool(runtime_config.get("error")) or bool(runtime_config.get("parse_errors"))
    runtime_state_error = bool(runtime_state.get("error")) or bool(runtime_state.get("parse_errors"))
    runtime_metrics_error = bool(runtime_metrics.get("error")) or int(runtime_metrics.get("parse_errors") or 0) > 0
    runtime_artifacts_present = bool(runtime_config.get("exists")) and bool(runtime_state.get("exists"))
    if runtime_artifacts_present and not (runtime_config_error or runtime_state_error or runtime_metrics_error):
        artifact_status = "healthy"
    elif (
        bool(runtime_config.get("exists"))
        or bool(runtime_state.get("exists"))
        or int(runtime_metrics.get("line_count") or 0) > 0
        or runtime_config_error
        or runtime_state_error
        or runtime_metrics_error
    ):
        artifact_status = "degraded"
    else:
        artifact_status = "unavailable"

    result = {
        "component": "strategy_app",
        "checked_at_ist": isoformat_ist(),
        "status": status,
        "process": {
            "running": process_running,
            "count": len(process_matches),
            "pids": [int(pid) for pid, _ in process_matches[:10]],
        },
        "redis": {
            "ok": redis_ok,
            "error": redis_error,
            "host": str(os.getenv("REDIS_HOST") or ""),
            "port": str(os.getenv("REDIS_PORT") or ""),
        },
        "runtime_artifacts": {
            "status": artifact_status,
            "paths": {
                "root": str(artifact_store.paths.root),
                "runtime_config": str(artifact_store.paths.config_path),
                "runtime_state": str(artifact_store.paths.state_path),
                "metrics": str(artifact_store.paths.metrics_path),
            },
            "config": runtime_config,
            "state": runtime_state,
            "metrics": runtime_metrics,
            "summary": summarize_runtime_artifacts(runtime_config.get("payload"), runtime_state.get("payload"), runtime_metrics),
        },
    }
    return result, code


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Health check for strategy_app")
    parser.add_argument("--topic", default=None)
    parser.add_argument("--artifact-dir", default=None, help="Runtime artifact directory (default: strategy app run dir)")
    parser.add_argument("--metrics-tail-lines", type=int, default=10, help="Number of metrics.jsonl lines to include in the tail")
    args = parser.parse_args(list(argv) if argv is not None else None)
    topic = str(args.topic or os.getenv("SNAPSHOT_V1_TOPIC") or os.getenv("LIVE_TOPIC") or "market:snapshot:v1")
    result, code = evaluate(topic=topic, artifact_dir=args.artifact_dir, metrics_tail_lines=int(args.metrics_tail_lines))
    print(json.dumps(result, ensure_ascii=False))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(run_cli())
