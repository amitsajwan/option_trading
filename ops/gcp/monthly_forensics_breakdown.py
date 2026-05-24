#!/usr/bin/env python3
"""Monthly exit/leg breakdown for a historical run."""
from __future__ import annotations

import os
import sys
from collections import defaultdict

from pymongo import MongoClient


def _payload(doc: dict) -> dict:
    p = doc.get("payload") if isinstance(doc.get("payload"), dict) else {}
    pos = p.get("position") if isinstance(p.get("position"), dict) else {}
    return pos or {}


def main() -> int:
    rid = sys.argv[1] if len(sys.argv) > 1 else ""
    if not rid:
        print("usage: monthly_forensics_breakdown.py RUN_ID", file=sys.stderr)
        return 2
    db = MongoClient(os.getenv("MONGO_URL", "mongodb://mongo:27017"))[os.getenv("MONGO_DB", "trading_ai")]
    opens: dict = {}
    closes: list = []
    for d in db.strategy_positions_historical.find({"run_id": rid}).sort("timestamp", 1):
        pl = _payload(d)
        pid = str(d.get("position_id") or pl.get("position_id") or "")
        ev = str(d.get("event") or pl.get("event") or "").upper()
        if ev == "POSITION_OPEN":
            opens[pid] = d
        elif ev == "POSITION_CLOSE" and pid in opens:
            pl = _payload(d)
            td = str(d.get("trade_date_ist") or pl.get("trade_date_ist") or "")[:10]
            if not td.startswith("2024-"):
                continue
            closes.append(
                {
                    "month": td[:7],
                    "day": td,
                    "dir": str(pl.get("direction") or ""),
                    "exit": str(pl.get("exit_reason") or ""),
                    "pnl": float(pl.get("pnl_pct") or 0),
                }
            )

    def pf(ps: list) -> float:
        w = sum(p for p in ps if p > 0)
        l = abs(sum(p for p in ps if p <= 0))
        return w / l if l > 0 else float("inf")

    print(f"run_id={rid}  closes={len(closes)}\n")
    print("=== Monthly totals ===")
    by_m: dict = defaultdict(list)
    for c in closes:
        by_m[c["month"]].append(c["pnl"])
    for m in sorted(by_m):
        ps = by_m[m]
        print(f"  {m}  n={len(ps):3d}  sum_cap%={sum(ps)*100:+.1f}%  avg={sum(ps)/len(ps)*100:+.2f}%  PF={pf(ps):.2f}")

    print("\n=== Monthly by exit (n, sum_cap%) ===")
    by_me: dict = defaultdict(lambda: defaultdict(list))
    for c in closes:
        by_me[c["month"]][c["exit"]].append(c["pnl"])
    for m in sorted(by_me):
        parts = []
        for ex in sorted(by_me[m]):
            ps = by_me[m][ex]
            parts.append(f"{ex}: n={len(ps)} sum={sum(ps)*100:+.1f}%")
        print(f"  {m}  " + " | ".join(parts))

    print("\n=== Monthly by direction (n, avg%, PF) ===")
    by_md: dict = defaultdict(lambda: defaultdict(list))
    for c in closes:
        by_md[c["month"]][c["dir"]].append(c["pnl"])
    for m in sorted(by_md):
        parts = []
        for d in sorted(by_md[m]):
            ps = by_md[m][d]
            parts.append(f"{d}: n={len(ps)} avg={sum(ps)/len(ps)*100:+.2f}% PF={pf(ps):.2f}")
        print(f"  {m}  " + " | ".join(parts))

    print("\n=== Worst 5 July days (sum cap %) ===")
    by_day: dict = defaultdict(list)
    for c in closes:
        if c["month"] == "2024-07":
            by_day[c["day"]].append(c["pnl"])
    for td, ps in sorted(by_day.items(), key=lambda x: sum(x[1]))[:5]:
        print(f"  {td}  n={len(ps)}  sum={sum(ps)*100:+.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
