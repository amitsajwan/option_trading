#!/usr/bin/env python3
"""Queue PBV1 smoke-window replays and poll until each completes."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8008/api/strategy/evaluation/runs"
WINDOWS = [
    ("2024-05-01", "2024-07-31", "may_jul_2024"),
    ("2024-08-01", "2024-10-31", "aug_oct_2024"),
]


def post_run(date_from: str, date_to: str) -> dict:
    payload = json.dumps(
        {"dataset": "historical", "date_from": date_from, "date_to": date_to, "speed": 0}
    ).encode()
    req = urllib.request.Request(
        API,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def run_status(run_id: str) -> str:
    url = f"{API}/{run_id}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return str(data.get("status") or "unknown").strip().lower()


def wait_run_id(run_id: str, label: str, max_minutes: int = 360) -> None:
    for i in range(max_minutes * 2):
        try:
            status = run_status(run_id)
        except urllib.error.URLError as exc:
            status = f"error:{exc}"
        print(f"[{label}] run_id={run_id} poll {i} status={status}", flush=True)
        if status in {"completed", "failed", "cancelled"}:
            return
        time.sleep(30)
    print(f"[{label}] TIMEOUT after {max_minutes}m", flush=True)


def main() -> int:
    for date_from, date_to, label in WINDOWS:
        print(f"QUEUE {label} {date_from} -> {date_to}", flush=True)
        try:
            result = post_run(date_from, date_to)
            print(json.dumps(result), flush=True)
            run_id = str(result.get("run_id") or "").strip()
            if not run_id:
                print(f"QUEUE_FAILED {label}: no run_id", flush=True)
                return 1
            wait_run_id(run_id, label)
        except Exception as exc:
            print(f"QUEUE_FAILED {label}: {exc}", flush=True)
            return 1
    print("ALL_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
