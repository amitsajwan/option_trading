#!/usr/bin/env python3
"""Queue a strategy eval replay run against local dashboard API."""
import json
import sys
import urllib.request

# speed = snapshots per minute (orchestrator sleep = 60/speed).
# speed=0 blasts Redis pub/sub and the historical consumer drops most events.
import os

_default_speed = float(os.getenv("REPLAY_EMIT_SNAPS_PER_MIN", "2400"))

payload = {
    "dataset": "historical",
    "date_from": sys.argv[1] if len(sys.argv) > 1 else "2024-05-01",
    "date_to": sys.argv[2] if len(sys.argv) > 2 else "2024-07-31",
    "speed": float(os.getenv("REPLAY_SPEED", str(_default_speed))),
}
req = urllib.request.Request(
    "http://127.0.0.1:8008/api/strategy/evaluation/runs",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    print(resp.read().decode())
