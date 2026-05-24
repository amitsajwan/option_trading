#!/usr/bin/env python3
"""One-line eval summary for a replay run_id."""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8008"


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=120) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    run_id = sys.argv[1]
    run = get(f"/api/strategy/evaluation/runs/{run_id}")
    df, dt = run.get("date_from"), run.get("date_to")
    q = urllib.parse.urlencode(
        {
            "dataset": "historical",
            "run_id": run_id,
            "date_from": df,
            "date_to": dt,
            "strategy": "PBV1_TOP3_THESIS",
        }
    )
    s = get(f"/api/strategy/evaluation/summary?{q}")
    counts = s.get("counts") or {}
    metrics = s.get("metrics") or s.get("summary") or {}
    exits = s.get("exit_reason_breakdown") or []
    print(f"run={run_id[:8]} status={run.get('status')} {df}..{dt}")
    print(f"  closed={counts.get('closed_trades')} wr={metrics.get('win_rate')} total_pnl_pct={metrics.get('total_net_pnl_pct')}")
    for row in exits[:8]:
        if isinstance(row, dict):
            print(f"  exit {row.get('exit_reason')}: {row.get('count')}")


if __name__ == "__main__":
    main()
