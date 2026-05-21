#!/usr/bin/env python3
"""Block until historical strategy + persistence consumers are subscribed."""
from __future__ import annotations

import subprocess
import sys
import time

COMPOSE_DIR = "/opt/option_trading"
COMPOSE_FILE = f"{COMPOSE_DIR}/docker-compose.yml"
ENV_FILE = f"{COMPOSE_DIR}/.env.compose"
LOCK_KEY = "strategy_app:consumer_lock:market:snapshot:v1:historical"
REDIS_CONTAINER = "option_trading-redis-1"

NEEDLES = {
    "strategy_app_historical": "strategy consumer subscribed topic=market:snapshot:v1:historical",
    "strategy_persistence_app_historical": "strategy persistence subscribed topics=",
}


def _run(cmd: list[str], *, use_sudo: bool = False) -> tuple[int, str]:
    full = ["sudo", *cmd] if use_sudo else cmd
    proc = subprocess.run(full, capture_output=True, text=True, check=False)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def clear_stale_consumer_lock() -> None:
    code, owner = _run(
        ["docker", "exec", REDIS_CONTAINER, "redis-cli", "GET", LOCK_KEY],
        use_sudo=True,
    )
    if code != 0 or not owner:
        return
    holder = owner.splitlines()[-1].strip()
    if not holder:
        return
    container_id = holder.split(":")[0]
    code, _ = _run(["docker", "inspect", "-f", "{{.State.Running}}", container_id], use_sudo=True)
    if code != 0:
        _run(
            ["docker", "exec", REDIS_CONTAINER, "redis-cli", "DEL", LOCK_KEY],
            use_sudo=True,
        )
        print(f"cleared stale consumer lock (dead holder {container_id[:12]})", flush=True)


def wait_ready(timeout_sec: int = 180) -> bool:
    clear_stale_consumer_lock()
    compose = [
        "docker",
        "compose",
        "--env-file",
        ENV_FILE,
        "-f",
        COMPOSE_FILE,
        "--profile",
        "historical",
    ]
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        ready = True
        for service, needle in NEEDLES.items():
            code, text = _run([*compose, "logs", "--tail", "120", service], use_sudo=True)
            if code != 0 or needle not in text:
                ready = False
                break
        if ready:
            print("historical consumers ready", flush=True)
            return True
        time.sleep(3)
    print("TIMEOUT: historical consumers not ready", flush=True)
    return False


def main() -> int:
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    return 0 if wait_ready(timeout) else 1


if __name__ == "__main__":
    sys.exit(main())
