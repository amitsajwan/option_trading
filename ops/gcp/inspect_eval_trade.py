#!/usr/bin/env python3
import json
import sys
import urllib.parse
import urllib.request

run_id = sys.argv[1]
date_from = sys.argv[2]
date_to = sys.argv[3]
BASE = "http://127.0.0.1:8008"
q = urllib.parse.urlencode(
    {
        "dataset": "historical",
        "run_id": run_id,
        "date_from": date_from,
        "date_to": date_to,
        "page": 1,
        "page_size": 5,
    }
)
with urllib.request.urlopen(BASE + "/api/strategy/evaluation/trades?" + q, timeout=120) as resp:
    data = json.loads(resp.read().decode())
for row in data.get("rows") or []:
    keys = sorted(row.keys())
    print("keys:", keys)
    for k in ("strategy", "strategy_name", "strategy_id", "rule_id", "exit_reason", "regime"):
        if k in row:
            print(f"  {k}={row[k]}")
    print("---")
