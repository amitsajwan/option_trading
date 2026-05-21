#!/usr/bin/env python3
"""Morning probe: PBV1 overnight replay trade counts vs rules cells."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8008"
RUNS = {
    "may_jul_2024": "7d6c8a92-8729-4904-a552-386682124c7b",
    "aug_oct_2024": "f8fdd6cb-1bf6-4f9d-8d63-380c4758d236",
}
RULES_TRADES = {
    ("may_jul_2024", "PBV1_TOP3_THESIS"): [
        "2024_05", "2024_06", "2024_07",
    ],
    ("aug_oct_2024", "PBV1_TOP3_THESIS"): [
        "2024_08", "2024_09", "2024_10",
    ],
}


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode())


def eval_trades(
    run_id: str,
    strategy: str,
    *,
    date_from: str,
    date_to: str,
) -> dict:
    params = urllib.parse.urlencode(
        {
            "dataset": "historical",
            "run_id": run_id,
            "strategy": strategy,
            "date_from": date_from,
            "date_to": date_to,
            "page": 1,
            "page_size": 500,
        }
    )
    return get(f"/api/strategy/evaluation/trades?{params}")


def eval_summary(
    run_id: str,
    strategy: str,
    *,
    date_from: str,
    date_to: str,
) -> dict:
    params = urllib.parse.urlencode(
        {
            "dataset": "historical",
            "run_id": run_id,
            "strategy": strategy,
            "date_from": date_from,
            "date_to": date_to,
        }
    )
    return get(f"/api/strategy/evaluation/summary?{params}")


def main() -> None:
    print("=== Overnight replays ===")
    for label, run_id in RUNS.items():
        run = get(f"/api/strategy/evaluation/runs/{run_id}")
        date_from = str(run.get("date_from") or "")
        date_to = str(run.get("date_to") or "")
        print(
            f"{label}: status={run.get('status')} "
            f"range={date_from}..{date_to} message={run.get('message')}"
        )
        for strat in ("PBV1_TOP3_THESIS",):
            try:
                t = eval_trades(
                    run_id, strat, date_from=date_from, date_to=date_to
                )
                s = eval_summary(
                    run_id, strat, date_from=date_from, date_to=date_to
                )
                closed = (t.get("counts") or {}).get("closed_trades")
                rows = len(t.get("rows") or [])
                pnl = (s.get("metrics") or s).get("avg_net_pnl_pct") or (s.get("summary") or {}).get("avg_net_pnl_pct")
                exits = (s.get("exit_reason_breakdown") or s.get("exit_reasons") or [])
                print(f"  {strat}: closed={closed} rows={rows} avg_pnl={pnl} exits={exits[:5]}")
            except Exception as exc:
                print(f"  {strat}: ERROR {exc}")

    print("\n=== Rules pipeline (monthly cells, holdout) ===")
    import os
    root = "/opt/option_trading/ml_pipeline_2/artifacts/rules_runs/playbook_v1_monthly_20260521/cells"
    for (label, rule), months in RULES_TRADES.items():
        total = 0
        for m in months:
            cell = f"{root}/{rule}_{m}_mechanical"
            p = os.path.join(cell, "trades.parquet")
            if not os.path.isfile(p):
                print(f"  {label} {m}: missing")
                continue
            try:
                import pandas as pd

                n = len(pd.read_parquet(p))
                total += n
                print(f"  {label} {m}: n={n}")
            except Exception as exc:
                print(f"  {label} {m}: {exc}")
        print(f"  {rule} {label} TOTAL rules n={total}")


if __name__ == "__main__":
    main()
