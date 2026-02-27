from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from contracts_app import find_matching_python_processes

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now_iso() -> str:
    return datetime.now(tz=IST).isoformat()


def _http_json(url: str, *, timeout_seconds: float) -> tuple[bool, int, Optional[dict[str, Any]], Optional[str], float]:
    start = time.monotonic()
    try:
        req = Request(url=url, method="GET")
        with urlopen(req, timeout=timeout_seconds) as resp:
            code = int(getattr(resp, "status", 200))
            body = resp.read().decode("utf-8", errors="replace")
            ms = (time.monotonic() - start) * 1000.0
            try:
                payload = json.loads(body) if body.strip() else {}
            except Exception:
                payload = None
            return (200 <= code < 300), code, payload, None, ms
    except URLError as exc:
        ms = (time.monotonic() - start) * 1000.0
        return False, 0, None, str(exc), ms
    except Exception as exc:  # pragma: no cover
        ms = (time.monotonic() - start) * 1000.0
        return False, 0, None, str(exc), ms


def evaluate(*, api_base: str, timeout_seconds: float) -> tuple[dict[str, Any], int]:
    base = str(api_base).rstrip("/")
    health_url = f"{base}/health"
    ok, status_code, payload, error, response_ms = _http_json(health_url, timeout_seconds=float(timeout_seconds))

    process_matches = find_matching_python_processes(["ingestion_app.main_live", "ingestion_app.runner --mode"])
    process_running = len(process_matches) > 0

    api_status = str((payload or {}).get("status") or "").lower() if isinstance(payload, dict) else ""
    redis_status = str((payload or {}).get("redis_status") or "") if isinstance(payload, dict) else ""
    dependency_state = "healthy" if ok and (not api_status or api_status in {"healthy", "ok"}) else "unhealthy"

    status = "healthy"
    code = 0
    if dependency_state != "healthy":
        status = "unhealthy"
        code = 2
    elif not process_running:
        status = "degraded"
        code = 1

    result = {
        "component": "ingestion_app",
        "checked_at_ist": _ist_now_iso(),
        "status": status,
        "process": {
            "running": process_running,
            "count": len(process_matches),
            "pids": [int(pid) for pid, _ in process_matches[:10]],
        },
        "api": {
            "url": health_url,
            "reachable": bool(ok),
            "status_code": int(status_code),
            "response_ms": round(float(response_ms), 2),
            "service_status": api_status or None,
            "redis_status": redis_status or None,
            "error": error,
        },
    }
    return result, code


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Health check for ingestion_app")
    parser.add_argument("--api-base", default="http://127.0.0.1:8004")
    parser.add_argument("--timeout-seconds", type=float, default=3.0)
    args = parser.parse_args(list(argv) if argv is not None else None)

    result, code = evaluate(api_base=str(args.api_base), timeout_seconds=float(args.timeout_seconds))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(run_cli())
