from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from typing import Any, Iterable, Optional

import redis

from contracts_app import find_matching_python_processes, redis_connection_kwargs


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def evaluate(*, topic: str) -> tuple[dict[str, Any], int]:
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

    result = {
        "component": "strategy_app",
        "checked_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
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
    }
    return result, code


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Health check for strategy_app")
    parser.add_argument("--topic", default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    topic = str(args.topic or os.getenv("SNAPSHOT_V1_TOPIC") or os.getenv("LIVE_TOPIC") or "market:snapshot:v1")
    result, code = evaluate(topic=topic)
    print(json.dumps(result, ensure_ascii=False))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(run_cli())
