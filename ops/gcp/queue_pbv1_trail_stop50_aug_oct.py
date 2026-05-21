#!/usr/bin/env python3
"""Queue Aug–Oct 2024 replay for PBV1 trail + 50% premium stop experiment."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8008/api/strategy/evaluation/runs"
DATE_FROM = "2024-08-01"
DATE_TO = "2024-10-31"
LABEL = "aug_oct_trail_stop50"


def post_run() -> dict:
    payload = json.dumps(
        {"dataset": "historical", "date_from": DATE_FROM, "date_to": DATE_TO, "speed": 0}
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


def main() -> int:
    print(f"QUEUE {LABEL} {DATE_FROM} -> {DATE_TO}", flush=True)
    try:
        result = post_run()
        print(json.dumps(result), flush=True)
        run_id = str(result.get("run_id") or "").strip()
        if not run_id:
            print("QUEUE_FAILED: no run_id", flush=True)
            return 1
        for i in range(360 * 2):
            try:
                status = run_status(run_id)
            except urllib.error.URLError as exc:
                status = f"error:{exc}"
            print(f"[{LABEL}] run_id={run_id} poll {i} status={status}", flush=True)
            if status in {"completed", "failed", "cancelled"}:
                print(f"DONE run_id={run_id}", flush=True)
                print(
                    f"EVAL_LINK=http://34.93.40.198:8008/app/?mode=eval&run_id={run_id}"
                    f"&date_from={DATE_FROM}&date_to={DATE_TO}",
                    flush=True,
                )
                return 0 if status == "completed" else 1
            time.sleep(30)
        print("TIMEOUT", flush=True)
        return 1
    except Exception as exc:
        print(f"QUEUE_FAILED: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
