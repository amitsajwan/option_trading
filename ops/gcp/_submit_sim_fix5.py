#!/usr/bin/env python3
"""One-off helper: submit buy-only sim run on VM."""
from __future__ import annotations

import json
import urllib.request

BODY = {
    "source_date": "2026-05-27",
    "source_coll": "phase1_market_snapshots",
    "label": "buy_only_debit_multi_fix7_2026_05_27",
    "speed": 30.0,
    "env_overrides": {
        "STRATEGY_PROFILE_ID": "debit_multi_v1",
        "DEPTH_FEED_ENABLED": "1",
        "ENTRY_ML_MIN_PROB": "0.65",
        "ML_ENTRY_BLOCK_CE": "0",
        "ML_ENTRY_BLOCK_PE": "0",
    },
}

payload = json.dumps(BODY).encode("utf-8")
req = urllib.request.Request(
    "http://127.0.0.1:8008/api/sim/runs",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    print(resp.read().decode("utf-8"))
