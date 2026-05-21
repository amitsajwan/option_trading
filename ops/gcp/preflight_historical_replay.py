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
LOCK_KEY = "strategy_app:consumer_lock:market:snapshot:v1:historical"


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
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
        ]
    )
    if code != 0:
        errors.append(f"docker compose ps failed: {ps_out[:300]}")
    elif HISTORICAL_SERVICE not in ps_out and "strategy_app_historical" not in ps_out:
        errors.append(f"{HISTORICAL_SERVICE} is not running (profile historical)")

    code, logs = _run(
        [
            "docker",
            "compose",
            "--env-file",
            ENV_FILE,
            "-f",
            COMPOSE_FILE,
            "logs",
            "--tail",
            "80",
            HISTORICAL_SERVICE,
        ]
    )
    if code == 0:
        if "subscribed topic=market:snapshot:v1:historical" not in logs:
            warnings.append("historical consumer may not be subscribed yet")
        if "PBV1_TOP3_THESIS" not in logs and "PBV1_TOP3" not in logs:
            warnings.append("recent logs do not show PBV1 strategy activity")
        if "profile=playbook_v1_paper_v1" not in logs and "PBV1_TOP3_THESIS" not in logs:
            if "TRENDING -> ['PBV1_TOP3_THESIS']" not in logs:
                warnings.append("router may not be on playbook_v1_paper_v1 (check build_run_metadata deploy)")

    code, rule_env = _run(
        [
            "docker",
            "exec",
            "option_trading-strategy_app_historical-1",
            "printenv",
            "PLAYBOOK_V1_RULE_PATH",
        ]
    )
    if code == 0 and rule_env:
        print(f"PLAYBOOK_V1_RULE_PATH={rule_env.splitlines()[-1]}", flush=True)
    else:
        warnings.append("PLAYBOOK_V1_RULE_PATH not readable from historical container")

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
