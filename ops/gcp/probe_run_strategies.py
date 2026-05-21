#!/usr/bin/env python3
"""List strategies and trade counts for an eval run."""
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
    run_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not run_id:
        print("usage: probe_run_strategies.py <run_id>")
        raise SystemExit(2)
    run = get(f"/api/strategy/evaluation/runs/{run_id}")
    date_from = str(run.get("date_from") or "")
    date_to = str(run.get("date_to") or "")
    print(f"run={run_id} status={run.get('status')} {date_from}..{date_to}")
    params = urllib.parse.urlencode(
        {
            "dataset": "historical",
            "run_id": run_id,
            "date_from": date_from,
            "date_to": date_to,
            "page": 1,
            "page_size": 500,
        }
    )
    trades = get(f"/api/strategy/evaluation/trades?{params}")
    rows = trades.get("rows") or []
    counts: dict[str, int] = {}
    for row in rows:
        strat = str(
            row.get("entry_strategy")
            or row.get("strategy")
            or row.get("strategy_name")
            or "?"
        )
        counts[strat] = counts.get(strat, 0) + 1
    print("closed_trades", (trades.get("counts") or {}).get("closed_trades"))
    print("rows", len(rows))
    for strat, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {strat}: {n}")
    if trades.get("detail"):
        print("detail:", trades["detail"])


if __name__ == "__main__":
    main()
