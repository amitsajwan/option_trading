#!/usr/bin/env python3
"""Block until historical strategy + persistence consumers are subscribed."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/option_trading"))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategy_app.runtime.consumer_lock import (  # noqa: E402
    holder_host_for_ops,
    lock_key_for_topic,
)

COMPOSE_DIR = str(REPO_ROOT)
COMPOSE_FILE = f"{COMPOSE_DIR}/docker-compose.yml"
ENV_FILE = f"{COMPOSE_DIR}/.env.compose"
HISTORICAL_TOPIC = os.environ.get(
    "HISTORICAL_TOPIC",
    "market:snapshot:v1:historical",
)
LOCK_KEY = lock_key_for_topic(HISTORICAL_TOPIC)

NEEDLES = {
    "strategy_app_historical": f"strategy consumer subscribed topic={HISTORICAL_TOPIC}",
    "strategy_persistence_app_historical": "strategy persistence subscribed topics=",
    "persistence_app_historical": "strategy persistence subscribed topics=",
}

_REDIS_CONTAINER_CANDIDATES = (
    os.environ.get("REDIS_CONTAINER", "").strip(),
    "option_trading-redis-1",
    "option_trading_redis_1",
)


def _run(cmd: list[str], *, use_sudo: bool = False) -> tuple[int, str]:
    full = ["sudo", *cmd] if use_sudo else cmd
    proc = subprocess.run(full, capture_output=True, text=True, check=False)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def discover_redis_container() -> str:
    for name in _REDIS_CONTAINER_CANDIDATES:
        if not name:
            continue
        code, _ = _run(["docker", "inspect", "-f", "{{.State.Running}}", name], use_sudo=True)
        if code == 0:
            return name
    return _REDIS_CONTAINER_CANDIDATES[1] or "option_trading-redis-1"


def _redis_get(key: str) -> str:
    container = discover_redis_container()
    code, out = _run(
        ["docker", "exec", container, "redis-cli", "GET", key],
        use_sudo=True,
    )
    if code != 0:
        return ""
    lines = [line.strip() for line in out.splitlines() if line.strip() and line.strip() != "(nil)"]
    return lines[-1] if lines else ""


def _redis_del(key: str) -> None:
    container = discover_redis_container()
    _run(["docker", "exec", container, "redis-cli", "DEL", key], use_sudo=True)


def _holder_container_running(holder_host: str) -> bool:
    if not holder_host:
        return False
    code, out = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", holder_host],
        use_sudo=True,
    )
    return code == 0 and out.strip().lower() == "true"


def clear_stale_consumer_lock(*, force: bool = False) -> None:
    """Drop lock when holder container is gone, or force before replay deploy."""
    owner = _redis_get(LOCK_KEY)
    if not owner:
        return
    holder_host = holder_host_for_ops(owner)
    if force or not _holder_container_running(holder_host):
        _redis_del(LOCK_KEY)
        reason = "force" if force else f"dead holder {holder_host[:12]}"
        print(f"cleared stale consumer lock ({reason})", flush=True)


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
    strategy_needle = NEEDLES["strategy_app_historical"]
    persistence_needle = NEEDLES["strategy_persistence_app_historical"]
    while time.time() < deadline:
        code, text = _run([*compose, "logs", "--tail", "300", "strategy_app_historical"], use_sudo=True)
        strategy_ok = code == 0 and strategy_needle in text
        persisted = False
        for svc in ("strategy_persistence_app_historical", "persistence_app_historical"):
            code, text = _run([*compose, "logs", "--tail", "300", svc], use_sudo=True)
            if code == 0 and persistence_needle in text:
                persisted = True
                break
        if strategy_ok:
            print(
                "historical consumers ready"
                if persisted
                else "strategy consumer ready (persistence log not confirmed)",
                flush=True,
            )
            return True
        time.sleep(3)
    print("TIMEOUT: historical consumers not ready", flush=True)
    return False


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        force = "--force" in sys.argv[2:]
        clear_stale_consumer_lock(force=force)
        return 0
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    return 0 if wait_ready(timeout) else 1


if __name__ == "__main__":
    sys.exit(main())
