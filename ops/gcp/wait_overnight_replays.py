#!/usr/bin/env python3
"""Poll specific eval run_ids until done (resume after mistaken early exit)."""
from __future__ import annotations

import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:8008/api/strategy/evaluation/runs"
RUNS = [
    ("7d6c8a92-8729-4904-a552-386682124c7b", "may_jul_2024"),
    ("f8fdd6cb-1bf6-4f9d-8d63-380c4758d236", "aug_oct_2024"),
]


def status(run_id: str) -> str:
    with urllib.request.urlopen(f"{API}/{run_id}", timeout=30) as resp:
        return str(json.loads(resp.read()).get("status", "")).lower()


def main() -> None:
    for run_id, label in RUNS:
        for i in range(720):
            st = status(run_id)
            print(f"{label} {run_id} poll={i} status={st}", flush=True)
            if st in {"completed", "failed", "cancelled"}:
                break
            time.sleep(30)
    print("WAIT_ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
