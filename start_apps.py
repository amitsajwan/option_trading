from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from contracts_app import find_matching_python_processes

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now_iso() -> str:
    return datetime.now(tz=IST).isoformat()


def _run_json_command(cmd: list[str], timeout_seconds: float) -> tuple[int, dict[str, Any]]:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(5.0, float(timeout_seconds)),
        )
    except Exception as exc:
        return 2, {
            "status": "unhealthy",
            "error": f"command_failed: {exc}",
            "command": cmd,
        }

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    payload: dict[str, Any]
    try:
        payload = json.loads(out) if out else {}
    except Exception:
        payload = {
            "status": "unhealthy",
            "parse_error": "non_json_output",
            "stdout": out,
            "stderr": err,
        }
    if err:
        payload.setdefault("stderr", err)
    payload.setdefault("exit_code", int(proc.returncode))
    payload.setdefault("command", cmd)
    return int(proc.returncode), payload


def _status_from_code(code: int) -> str:
    if int(code) == 0:
        return "healthy"
    if int(code) == 1:
        return "degraded"
    return "unhealthy"


def _http_json(url: str, *, timeout_seconds: float) -> tuple[bool, int, dict[str, Any] | None, str | None, float]:
    start = time.monotonic()
    try:
        req = Request(url=url, method="GET")
        with urlopen(req, timeout=max(0.5, float(timeout_seconds))) as resp:
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
    except Exception as exc:
        ms = (time.monotonic() - start) * 1000.0
        return False, 0, None, str(exc), ms


def _detached_popen_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        detached_process = 0x00000008
        create_new_process_group = 0x00000200
        create_no_window = 0x08000000
        return {
            "creationflags": detached_process | create_new_process_group | create_no_window,
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


def _launch_detached(*, cmd: list[str], run_dir: str, cwd: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    out_dir = Path(run_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "stdout.log"
    stderr_path = out_dir / "stderr.log"
    meta_path = out_dir / "process.json"
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env or os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            **_detached_popen_kwargs(),
        )
    meta = {
        "component": "dashboard",
        "pid": int(proc.pid),
        "command": cmd,
        "started_at_ist": _ist_now_iso(),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _port_from_base_url(base_url: str, default: int = 8002) -> int:
    try:
        parsed = urlparse(str(base_url).strip())
        if parsed.port:
            return int(parsed.port)
    except Exception:
        pass
    return int(default)


def _start_or_verify_dashboard(
    *,
    dashboard_api_base: str,
    ingestion_api_base: str,
    health_timeout_seconds: float,
) -> tuple[int, dict[str, Any]]:
    health_url = f"{str(dashboard_api_base).rstrip('/')}/api/health"
    running = find_matching_python_processes(["start_dashboard.py"])
    run_dir = str(Path(".run/dashboard").resolve())
    controls = {
        "logs_dir": run_dir,
        "stop_command": "python -m stop_apps --include-dashboard",
        "health_url": health_url,
    }

    if running:
        ok, status_code, payload, error, response_ms = _http_json(health_url, timeout_seconds=3.0)
        code = 0 if ok else 2
        result = {
            "component": "dashboard",
            "checked_at_ist": _ist_now_iso(),
            "status": "healthy" if ok else "unhealthy",
            "process": {
                "running": True,
                "count": len(running),
                "pids": [int(pid) for pid, _ in running[:10]],
            },
            "api": {
                "url": health_url,
                "reachable": bool(ok),
                "status_code": int(status_code),
                "response_ms": round(float(response_ms), 2),
                "error": error,
                "payload": payload if isinstance(payload, dict) else None,
            },
            "launcher": {
                "component": "dashboard",
                "action": "already_running",
                "pids": [int(pid) for pid, _ in running[:10]],
                "duplicate_processes_detected": len(running) > 1,
                "run_dir": run_dir,
            },
            "controls": controls,
        }
        return code, result

    dashboard_dir = Path("market_data_dashboard").resolve()
    cmd = [sys.executable, "start_dashboard.py"]
    env = os.environ.copy()
    env["MARKET_DATA_API_URL"] = str(ingestion_api_base)
    env["DASHBOARD_PORT"] = str(_port_from_base_url(dashboard_api_base, default=8002))
    launch_meta = _launch_detached(cmd=cmd, run_dir=run_dir, cwd=str(dashboard_dir), env=env)

    deadline = time.monotonic() + max(2.0, float(health_timeout_seconds))
    ok = False
    status_code = 0
    payload = None
    error = None
    response_ms = 0.0
    while time.monotonic() < deadline:
        ok, status_code, payload, error, response_ms = _http_json(health_url, timeout_seconds=2.0)
        if ok:
            break
        time.sleep(1.0)

    code = 0 if ok else 2
    result = {
        "component": "dashboard",
        "checked_at_ist": _ist_now_iso(),
        "status": "healthy" if ok else "unhealthy",
        "process": {
            "running": True,
            "count": 1,
            "pids": [int(launch_meta.get("pid", 0))] if launch_meta.get("pid") else [],
        },
        "api": {
            "url": health_url,
            "reachable": bool(ok),
            "status_code": int(status_code),
            "response_ms": round(float(response_ms), 2),
            "error": error,
            "payload": payload if isinstance(payload, dict) else None,
        },
        "launcher": launch_meta,
        "controls": controls,
    }
    return code, result


def run_cli() -> int:
    parser = argparse.ArgumentParser(description="Start process-app components sequentially with health output")
    parser.add_argument("--instrument", default="BANKNIFTY26MARFUT")
    parser.add_argument("--ingestion-api-base", default="http://127.0.0.1:8004")
    parser.add_argument("--snapshot-dashboard-api-base", default="http://127.0.0.1:8002")
    parser.add_argument("--include-dashboard", action="store_true")
    parser.add_argument("--snapshot-events-path", default=".run/snapshot_app/events.jsonl")
    parser.add_argument("--snapshot-ohlc-limit", type=int, default=300)
    parser.add_argument("--snapshot-max-age-seconds", type=float, default=900.0)
    parser.add_argument("--persistence-max-age-seconds", type=float, default=900.0)
    parser.add_argument("--health-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--skip-ingestion", action="store_true")
    parser.add_argument("--skip-snapshot", action="store_true")
    parser.add_argument("--skip-persistence", action="store_true")
    args = parser.parse_args()

    components: list[dict[str, Any]] = []

    if not bool(args.skip_ingestion):
        ingestion_cmd = [
            sys.executable,
            "-m",
            "ingestion_app.main_live",
            "--mode",
            "live",
            "--start-collectors",
            "--api-base",
            str(args.ingestion_api_base),
            "--health-timeout-seconds",
            str(float(args.health_timeout_seconds)),
        ]
        code, payload = _run_json_command(ingestion_cmd, timeout_seconds=float(args.health_timeout_seconds) + 10.0)
        components.append(
            {
                "component": "ingestion_app",
                "start_status": _status_from_code(code),
                "exit_code": int(code),
                "result": payload,
            }
        )

    if bool(args.include_dashboard):
        code, payload = _start_or_verify_dashboard(
            dashboard_api_base=str(args.snapshot_dashboard_api_base),
            ingestion_api_base=str(args.ingestion_api_base),
            health_timeout_seconds=float(args.health_timeout_seconds),
        )
        components.append(
            {
                "component": "dashboard",
                "start_status": _status_from_code(code),
                "exit_code": int(code),
                "result": payload,
            }
        )

    if not bool(args.skip_snapshot):
        snapshot_cmd = [
            sys.executable,
            "-m",
            "snapshot_app.main_live",
            "--instrument",
            str(args.instrument),
            "--dashboard-api-base",
            str(args.snapshot_dashboard_api_base),
            "--timeout-seconds",
            str(float(args.health_timeout_seconds)),
            "--health-timeout-seconds",
            str(float(args.health_timeout_seconds)),
            "--health-max-age-seconds",
            str(float(args.snapshot_max_age_seconds)),
            "--ohlc-limit",
            str(max(60, int(args.snapshot_ohlc_limit))),
            "--out-jsonl",
            str(args.snapshot_events_path),
        ]
        code, payload = _run_json_command(snapshot_cmd, timeout_seconds=float(args.health_timeout_seconds) + 10.0)
        components.append(
            {
                "component": "snapshot_app",
                "start_status": _status_from_code(code),
                "exit_code": int(code),
                "result": payload,
            }
        )

    if not bool(args.skip_persistence):
        persistence_cmd = [
            sys.executable,
            "-m",
            "persistence_app.main_snapshot_consumer",
            "--health-timeout-seconds",
            str(float(args.health_timeout_seconds)),
            "--health-max-age-seconds",
            str(float(args.persistence_max_age_seconds)),
        ]
        code, payload = _run_json_command(persistence_cmd, timeout_seconds=float(args.health_timeout_seconds) + 10.0)
        components.append(
            {
                "component": "persistence_app",
                "start_status": _status_from_code(code),
                "exit_code": int(code),
                "result": payload,
            }
        )

    if not components:
        payload = {
            "checked_at_ist": _ist_now_iso(),
            "status": "unhealthy",
            "error": "nothing_to_start",
            "controls": {"stop_all_command": "python -m stop_apps"},
        }
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return 2

    max_code = max(int(c["exit_code"]) for c in components)
    overall_status = _status_from_code(max_code)
    health_commands = [
        f"python -m ingestion_app.health --api-base {str(args.ingestion_api_base)}",
        f"python -m snapshot_app.health --events-path {str(args.snapshot_events_path)} --max-age-seconds {float(args.snapshot_max_age_seconds)}",
        f"python -m persistence_app.health --max-age-seconds {float(args.persistence_max_age_seconds)}",
    ]
    if bool(args.include_dashboard):
        health_commands.append(f"curl {str(args.snapshot_dashboard_api_base).rstrip('/')}/api/health")

    payload = {
        "checked_at_ist": _ist_now_iso(),
        "status": overall_status,
        "components": components,
        "controls": {
            "stop_all_command": "python -m stop_apps",
            "health_commands": health_commands,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return int(max_code)


if __name__ == "__main__":
    raise SystemExit(run_cli())
