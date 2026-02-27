from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from contracts_app import find_matching_python_processes

from .health import evaluate as evaluate_health

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now_iso() -> str:
    return datetime.now(tz=IST).isoformat()


def _build_runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    # Transitional compatibility while market_data package still lives under market_data/src.
    src_root = (Path(__file__).resolve().parents[1] / "market_data" / "src").resolve()
    if src_root.exists():
        src_text = str(src_root)
        current = str(env.get("PYTHONPATH") or "")
        parts = [p for p in current.split(os.pathsep) if p] if current else []
        if src_text not in parts:
            env["PYTHONPATH"] = src_text if not current else f"{src_text}{os.pathsep}{current}"
    return env


def _detached_popen_kwargs() -> dict:
    if os.name == "nt":
        detached_process = 0x00000008
        create_new_process_group = 0x00000200
        create_no_window = 0x08000000
        return {
            "creationflags": detached_process | create_new_process_group | create_no_window,
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


def _launch_detached(*, cmd: list[str], run_dir: str) -> dict:
    out_dir = Path(run_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "stdout.log"
    stderr_path = out_dir / "stderr.log"
    meta_path = out_dir / "process.json"
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        proc = subprocess.Popen(
            cmd,
            env=_build_runtime_env(),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            **_detached_popen_kwargs(),
        )
    meta = {
        "component": "ingestion_app",
        "pid": int(proc.pid),
        "command": cmd,
        "started_at_ist": _ist_now_iso(),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Top-level Ingestion process wrapper")
    parser.add_argument("--mode", default="live", choices=["live", "historical", "mock"])
    parser.add_argument("--start-collectors", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--run-dir", default=".run/ingestion_app")
    parser.add_argument("--api-base", default="http://127.0.0.1:8004")
    parser.add_argument("--health-timeout-seconds", type=float, default=30.0)
    args, passthrough = parser.parse_known_args(list(argv) if argv is not None else None)

    cmd = [sys.executable, "-m", "ingestion_app.runner", "--mode", str(args.mode)]
    if bool(args.start_collectors):
        cmd.append("--start-collectors")
    if bool(args.skip_validation):
        cmd.append("--skip-validation")
    cmd.extend(passthrough)

    if bool(args.foreground):
        env = _build_runtime_env()
        proc = subprocess.run(cmd, env=env, check=False)
        return int(proc.returncode)

    controls = {
        "stop_command": "python -m ingestion_app.stop",
        "health_command": f"python -m ingestion_app.health --api-base {str(args.api_base)}",
        "logs_dir": str(Path(args.run_dir).resolve()),
    }

    self_pid = int(os.getpid())
    running = [
        (pid, cmdline)
        for pid, cmdline in find_matching_python_processes(["ingestion_app.runner", "ingestion_app.main_live --mode"])
        if int(pid) != self_pid
    ]
    if running:
        pids = [int(pid) for pid, _ in running[:20]]
        result, code = evaluate_health(
            api_base=str(args.api_base),
            timeout_seconds=min(3.0, max(0.5, float(args.health_timeout_seconds))),
        )
        result["launcher"] = {
            "component": "ingestion_app",
            "action": "already_running",
            "pids": pids,
            "duplicate_processes_detected": len(pids) > 1,
            "run_dir": str(Path(args.run_dir).resolve()),
        }
        result["controls"] = controls
        print(json.dumps(result, ensure_ascii=False, default=str))
        return int(code)

    launch_meta = _launch_detached(cmd=cmd, run_dir=str(args.run_dir))
    deadline = time.monotonic() + max(1.0, float(args.health_timeout_seconds))
    result = None
    code = 2
    while time.monotonic() < deadline:
        result, code = evaluate_health(
            api_base=str(args.api_base),
            timeout_seconds=min(3.0, max(0.5, float(args.health_timeout_seconds))),
        )
        # Accept healthy/degraded as successful non-blocking start.
        if code in (0, 1):
            break
        time.sleep(1.0)

    if result is None:
        result, code = evaluate_health(
            api_base=str(args.api_base),
            timeout_seconds=min(3.0, max(0.5, float(args.health_timeout_seconds))),
        )

    result["launcher"] = launch_meta
    result["controls"] = controls
    print(json.dumps(result, ensure_ascii=False, default=str))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(run_cli())
