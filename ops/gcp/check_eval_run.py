#!/usr/bin/env python3
"""Print closed-trade count and sample rows for an eval run_id."""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8008"


def get(path: str) -> dict | list:
    with urllib.request.urlopen(BASE + path, timeout=120) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    run_id = sys.argv[1]
    run = get(f"/api/strategy/evaluation/runs/{run_id}")
    df, dt = run.get("date_from"), run.get("date_to")
    status = run.get("status")
    q = urllib.parse.urlencode(
        {
            "dataset": "historical",
            "run_id": run_id,
            "date_from": df,
            "date_to": dt,
        }
    )
    summary = get(f"/api/strategy/evaluation/summary?{q}")
    counts = summary.get("counts") or {}
    closed = int(counts.get("closed_trades") or 0)
    print(f"run={run_id[:8]} status={status} {df}..{dt} closed={closed}")
    if closed == 0:
        print("NO_CLOSED_TRADES")
        return
    tq = urllib.parse.urlencode(
        {
            "dataset": "historical",
            "run_id": run_id,
            "date_from": df,
            "date_to": dt,
            "limit": "10",
        }
    )
    trades = get(f"/api/strategy/evaluation/trades?{tq}")
    rows = trades if isinstance(trades, list) else trades.get("trades") or trades.get("rows") or []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        print(
            f"  {row.get('entry_strategy')} {row.get('direction')} "
            f"opt_pnl={row.get('pnl_pct_net')} cap_pnl={row.get('capital_pnl_pct')} "
            f"exit={row.get('exit_reason')}"
        )


if __name__ == "__main__":
    main()
