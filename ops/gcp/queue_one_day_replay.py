#!/usr/bin/env python3
"""Queue a one-day historical replay (waits for consumers first)."""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from wait_historical_consumers import wait_ready

API = "http://127.0.0.1:8008/api/strategy/evaluation/runs"


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else "2024-10-31"
    if not wait_ready(180):
        print("abort: strategy_app_historical not subscribed", flush=True)
        return 1
    payload = json.dumps(
        {"dataset": "historical", "date_from": date, "date_to": date, "speed": 0}
    ).encode()
    req = urllib.request.Request(
        API, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())
    print(json.dumps(result, indent=2))
    run_id = str(result.get("run_id") or "")
    if not run_id:
        return 1
    for i in range(120):
        with urllib.request.urlopen(f"{API}/{run_id}", timeout=30) as resp:
            status = str(json.loads(resp.read().decode()).get("status") or "")
        print(f"poll {i} status={status}", flush=True)
        if status in {"completed", "failed", "cancelled"}:
            break
        time.sleep(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
