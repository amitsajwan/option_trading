#!/usr/bin/env python3
"""Pre-flight checks before queuing a historical strategy eval replay."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

API_BASE = "http://127.0.0.1:8008"
COMPOSE_DIR = "/opt/option_trading"
COMPOSE_FILE = f"{COMPOSE_DIR}/docker-compose.yml"
ENV_FILE = f"{COMPOSE_DIR}/.env.compose"
HISTORICAL_SERVICE = "strategy_app_historical"
from wait_historical_consumers import wait_ready


def _run(cmd: list[str], *, use_sudo: bool = False) -> tuple[int, str]:
    full = ["sudo", *cmd] if use_sudo else cmd
    proc = subprocess.run(full, capture_output=True, text=True, check=False)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _http_get(path: str) -> dict:
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=15) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    code, ps_out = _run(
        [
            "docker",
            "compose",
            "--env-file",
            ENV_FILE,
            "-f",
            COMPOSE_FILE,
            "--profile",
            "historical",
            "ps",
            "--format",
            "json",
            HISTORICAL_SERVICE,
        ],
        use_sudo=True,
    )
    if code != 0:
        errors.append(f"docker compose ps failed: {ps_out[:300]}")
    elif HISTORICAL_SERVICE not in ps_out and "strategy_app_historical" not in ps_out:
        errors.append(f"{HISTORICAL_SERVICE} is not running (profile historical)")

    code, profile_env = _run(
        [
            "docker",
            "exec",
            "option_trading-strategy_app_historical-1",
            "printenv",
            "STRATEGY_PROFILE_ID",
            "STRATEGY_ENGINE",
        ],
        use_sudo=True,
    )
    if code == 0 and profile_env:
        for line in profile_env.splitlines():
            if line.strip():
                print(line.strip(), flush=True)
    else:
        warnings.append("STRATEGY_PROFILE_ID not readable from historical container")

    if not wait_ready(30):
        errors.append(
            "historical consumers not subscribed (replay would emit snapshots with 0 trades)"
        )

    try:
        health = _http_get("/api/health")
        print(f"dashboard_health={health.get('status', health)}", flush=True)
    except urllib.error.URLError as exc:
        errors.append(f"dashboard API unreachable: {exc}")

    try:
        runs = _http_get("/api/strategy/evaluation/runs?dataset=historical&limit=1")
        print(f"eval_api_ok rows={len(runs.get('rows') or [])}", flush=True)
    except urllib.error.URLError as exc:
        errors.append(f"eval runs API unreachable: {exc}")

    for msg in warnings:
        print(f"WARN: {msg}", flush=True)
    for msg in errors:
        print(f"ERROR: {msg}", flush=True)

    if errors:
        print("PREFLIGHT_FAIL", flush=True)
        return 1
    print("PREFLIGHT_OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
