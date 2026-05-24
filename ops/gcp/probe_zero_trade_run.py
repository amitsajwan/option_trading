#!/usr/bin/env python3
"""Diagnose an eval run with 0 closed trades."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8008"
COMPOSE = [
    "sudo",
    "docker",
    "compose",
    "--env-file",
    "/opt/option_trading/.env.compose",
    "-f",
    "/opt/option_trading/docker-compose.yml",
    "--profile",
    "historical",
]


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=120) as resp:
        return json.loads(resp.read().decode())


def tail_logs(service: str, n: int = 80) -> str:
    proc = subprocess.run(
        [*COMPOSE, "logs", "--tail", str(n), service],
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def find_run(prefix: str) -> dict:
    data = get("/api/strategy/evaluation/runs?dataset=historical&limit=50")
    for row in data.get("rows") or []:
        if str(row.get("run_id", "")).startswith(prefix):
            return row
    return {}


def main() -> None:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "d7cae505"
    row = find_run(prefix)
    if not row:
        print(f"no run with prefix {prefix}")
        sys.exit(1)
    run_id = row["run_id"]
    print("=== run doc ===")
    print(json.dumps(row, indent=2))
    q = urllib.parse.urlencode(
        {
            "dataset": "historical",
            "run_id": run_id,
            "date_from": row.get("date_from"),
            "date_to": row.get("date_to"),
        }
    )
    summary = get(f"/api/strategy/evaluation/summary?{q}")
    print("=== summary counts ===")
    print(json.dumps(summary.get("counts") or {}, indent=2))
    state = get("/api/strategy/current/state?mode=replay&latest_n=0")
    rc = state.get("runtime_config") or {}
    print("=== replay runtime_config ===")
    print(f"profile={rc.get('strategy_profile_id')} engine={rc.get('engine')}")
    print("=== historical logs (tail) ===")
    logs = tail_logs("strategy_app_historical", 60)
    for needle in (
        "consumer lock",
        "subscribed topic",
        "Replay finished",
        "trader_master",
        "deterministic engine",
        "morning_briefing",
        "gate_entry",
        "BLOCK",
        "policy_block",
    ):
        hits = [ln for ln in logs.splitlines() if needle.lower() in ln.lower()]
        if hits:
            print(f"-- {needle} ({len(hits)} lines) --")
            for ln in hits[-5:]:
                print(ln[-200:])
    pers = tail_logs("strategy_persistence_app_historical", 30)
    if "subscribed" in pers:
        print("=== persistence subscribed (ok) ===")
    else:
        print("=== persistence may not be subscribed ===")
        print(pers[-1500:])


if __name__ == "__main__":
    main()
