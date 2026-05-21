#!/usr/bin/env python3
"""Queue a strategy eval replay run against local dashboard API."""
import json
import sys
import urllib.request

payload = {
    "dataset": "historical",
    "date_from": sys.argv[1] if len(sys.argv) > 1 else "2024-05-01",
    "date_to": sys.argv[2] if len(sys.argv) > 2 else "2024-07-31",
    "speed": 0,
}
req = urllib.request.Request(
    "http://127.0.0.1:8008/api/strategy/evaluation/runs",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    print(resp.read().decode())
