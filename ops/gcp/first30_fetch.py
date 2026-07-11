#!/usr/bin/env python3
"""Print today's first 09:15-09:45 index bars as JSON (helper for the dev-box
first30 check). Runs in a disposable container on the compose network:
  docker run --rm --network option_trading_default \
    -v /opt/option_trading/ops/gcp/first30_fetch.py:/f.py \
    python:3.12-slim python /f.py BANKNIFTY 2026-07-13
Exists as a FILE because inline python through double-ssh quoting breaks
(2026-07-11, again). stdlib only.
"""
import json
import sys
import urllib.request

inst, day = sys.argv[1], sys.argv[2]
url = (f"http://ingestion_app:8004/api/v1/historical/day/{inst}"
       f"?date={day}&strikes=1&interval=1")
r = json.load(urllib.request.urlopen(url, timeout=300))
print(json.dumps((r.get("index_bars") or [])[:35]))
