#!/usr/bin/env python3
import json
import sys
import urllib.request

run_id = sys.argv[1]
payload = json.dumps({"run_id": run_id}).encode("utf-8")
req = urllib.request.Request(
    f"http://127.0.0.1:8008/api/sim/runs/{run_id}/cancel",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    print(resp.read().decode("utf-8"))
