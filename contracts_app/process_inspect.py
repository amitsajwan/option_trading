from __future__ import annotations

import json
import os
import subprocess
from typing import Iterable, List, Tuple


def _windows_process_list() -> List[Tuple[int, str]]:
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return []
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    rows = data if isinstance(data, list) else [data]
    out: List[Tuple[int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cmdline = str(row.get("CommandLine") or "").strip()
        pid_raw = row.get("ProcessId")
        try:
            pid = int(pid_raw)
        except Exception:
            continue
        if not cmdline:
            continue
        out.append((pid, cmdline))
    return out


def _posix_process_list() -> List[Tuple[int, str]]:
    proc = subprocess.run(
        ["ps", "-eo", "pid,args", "--no-headers"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    out: List[Tuple[int, str]] = []
    for line in (proc.stdout or "").splitlines():
        text = line.strip()
        if not text:
            continue
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except Exception:
            continue
        out.append((pid, parts[1]))
    return out


def list_processes() -> List[Tuple[int, str]]:
    if os.name == "nt":
        return _windows_process_list()
    return _posix_process_list()


def find_matching_processes(tokens: Iterable[str]) -> List[Tuple[int, str]]:
    checks = [str(t).strip().lower() for t in tokens if str(t).strip()]
    if not checks:
        return []
    out: List[Tuple[int, str]] = []
    for pid, cmdline in list_processes():
        text = cmdline.lower()
        if any(token in text for token in checks):
            out.append((pid, cmdline))
    return out


def _is_python_cmdline(cmdline: str) -> bool:
    text = str(cmdline or "").strip().lower()
    if not text:
        return False
    first = text.split(" ", 1)[0].strip().strip('"').strip("'")
    return first.endswith("python.exe") or first.endswith("\\python") or first == "python" or first == "python.exe"


def find_matching_python_processes(tokens: Iterable[str]) -> List[Tuple[int, str]]:
    matches = find_matching_processes(tokens)
    return [(pid, cmdline) for pid, cmdline in matches if _is_python_cmdline(cmdline)]
