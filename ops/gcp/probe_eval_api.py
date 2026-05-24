#!/usr/bin/env python3
"""Probe eval API the same way the UI does."""
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8008"


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode())


def q(**kwargs) -> str:
    return urllib.parse.urlencode({k: v for k, v in kwargs.items() if v is not None and v != ""})


cases = [
    ("ui_default_jan", q(dataset="historical", date_from="2024-01-01", date_to="2024-01-31", strategy="R1S_TOP3_SHORT_CE")),
    ("may_jul_r1s", q(dataset="historical", date_from="2024-05-01", date_to="2024-07-31", strategy="R1S_TOP3_SHORT_CE")),
    ("may_jul_all", q(dataset="historical", date_from="2024-05-01", date_to="2024-07-31")),
    ("may_jul_run", q(dataset="historical", date_from="2024-05-01", date_to="2024-07-31", strategy="R1S_TOP3_SHORT_CE", run_id="bbc529e4-f9b7-402c-831b-fdcb2f9a52f2")),
]
for name, params in cases:
    try:
        trades = get(f"/api/strategy/evaluation/trades?{params}&page=1&page_size=50")
        summary = get(f"/api/strategy/evaluation/summary?{params}")
        print(
            name,
            "rows=",
            len(trades.get("rows") or []),
            "total_closed=",
            (trades.get("counts") or {}).get("closed_trades"),
            "resolved_run=",
            trades.get("resolved_run_id"),
            "summary_closed=",
            (summary.get("counts") or {}).get("closed_trades"),
        )
        if trades.get("detail"):
            print("  detail:", trades["detail"])
    except Exception as exc:
        print(name, "ERROR", exc)

runs = get("/api/strategy/evaluation/runs")
print("runs:", len(runs if isinstance(runs, list) else runs.get("runs", [])))
