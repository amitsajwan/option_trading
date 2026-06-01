#!/usr/bin/env python3
import json
import time
import urllib.request

RUN_ID = "4b264cbc-a38a-421b-95de-71827941ce1f"
URL = f"http://127.0.0.1:8008/api/sim/runs/{RUN_ID}"

for i in range(20):
    with urllib.request.urlopen(URL, timeout=15) as resp:
        row = json.loads(resp.read().decode("utf-8"))
    status = str(row.get("status") or "")
    print(f"check {i + 1} status={status}")
    if status in {"completed", "failed", "cancelled"}:
        break
    time.sleep(60)

import subprocess

subprocess.run(["python3", "/tmp/_check_sim_run.py", RUN_ID], check=False)
