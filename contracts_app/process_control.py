from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Iterable, List, Tuple

from .process_inspect import find_matching_python_processes, list_processes


def _alive_pids(candidates: List[int]) -> List[int]:
    if not candidates:
        return []
    pid_set = set(int(p) for p in candidates)
    alive = [pid for pid, _ in list_processes() if pid in pid_set]
    return sorted(set(int(pid) for pid in alive))


def _terminate_pid(pid: int, *, force: bool) -> None:
    if os.name == "nt":
        cmd = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            cmd.append("/F")
        subprocess.run(cmd, check=False, capture_output=True, text=True)
        return
    try:
        os.kill(int(pid), signal.SIGKILL if force else signal.SIGTERM)
    except Exception:
        return


def terminate_matching_processes(
    *,
    tokens: Iterable[str],
    timeout_seconds: float = 5.0,
    force_after_timeout: bool = True,
) -> dict:
    initial_matches: List[Tuple[int, str]] = find_matching_python_processes(tokens)
    current_pid = os.getpid()
    target_pids = sorted(set(int(pid) for pid, _ in initial_matches if int(pid) != int(current_pid)))
    if not target_pids:
        return {
            "found": [],
            "terminated": [],
            "remaining": [],
            "status": "not_running",
        }

    for pid in target_pids:
        _terminate_pid(pid, force=False)

    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    remaining = _alive_pids(target_pids)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.25)
        remaining = _alive_pids(target_pids)

    if remaining and force_after_timeout:
        for pid in remaining:
            _terminate_pid(pid, force=True)
        time.sleep(0.5)
        remaining = _alive_pids(target_pids)

    terminated = [pid for pid in target_pids if pid not in set(remaining)]
    return {
        "found": target_pids,
        "terminated": terminated,
        "remaining": remaining,
        "status": "stopped" if not remaining else "partial",
    }
