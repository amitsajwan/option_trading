#!/usr/bin/env python3
"""E2-S7: Diagnose replay coverage (trades/votes/blockers by month and run)."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo required", file=sys.stderr)
    sys.exit(2)

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
DB_NAME = os.environ.get("MONGO_DB", "trading_ai")

_SESSION_SUMMARY_CANDIDATES = [
    Path("/app/.run/strategy_app_historical/session_summary.jsonl"),
    Path("/opt/option_trading/.run/strategy_app_historical/session_summary.jsonl"),
]


def _read_session_summary() -> list[dict]:
    for p in _SESSION_SUMMARY_CANDIDATES:
        if p.exists():
            records = []
            try:
                with p.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            try:
                                records.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            except OSError:
                continue
            return records
    return []


def _show_session_summary() -> None:
    records = _read_session_summary()
    if not records:
        print("\n  [session_summary.jsonl] not found or empty")
        return
    print(f"\n  [session_summary.jsonl] {len(records)} records")
    # Show carry that would be loaded at each OOS window start
    windows = {
        "oos_primary start (2024-05-01)": date(2024, 5, 1),
        "in_sample start (2024-08-01)": date(2024, 8, 1),
        "oos_secondary start (2023-05-01)": date(2023, 5, 1),
    }
    for label, start in windows.items():
        best = None
        best_date = None
        for rec in records:
            try:
                rd = date.fromisoformat(str(rec.get("trade_date", "")))
            except (ValueError, TypeError):
                continue
            if rd >= start:
                continue
            if best_date is None or rd > best_date:
                best = rec
                best_date = rd
        if best is None:
            print(f"    {label}: no prior record → fresh start")
        else:
            cl = int(best.get("consecutive_losses_at_close", 0))
            pnl = float(best.get("session_pnl_pct", 0)) * 100
            print(
                f"    {label}: carry from {best_date}  "
                f"consec_losses={cl}  session_pnl={pnl:+.2f}%"
            )
    # Also show tail of records to see recent contamination
    tail = records[-8:] if len(records) > 8 else records
    print(f"  Last {len(tail)} records:")
    for r in tail:
        cl = int(r.get("consecutive_losses_at_close", 0))
        print(
            f"    {r.get('trade_date')}  trades={r.get('trades', 0)}  "
            f"consec_losses={cl}  pnl={float(r.get('session_pnl_pct', 0)) * 100:+.2f}%"
        )


def fetch_run(run_id: str) -> dict:
    with urllib.request.urlopen(
        f"http://127.0.0.1:8008/api/strategy/evaluation/runs/{run_id}",
        timeout=20,
    ) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    run_ids = sys.argv[1:] if len(sys.argv) > 1 else []
    if not run_ids:
        print("Usage: diagnose_oos_replay_coverage.py RUN_ID [RUN_ID ...]", file=sys.stderr)
        return 2

    print("=" * 72)
    print("  SESSION SUMMARY CARRY STATE (cross-replay contamination check)")
    print("=" * 72)
    _show_session_summary()

    db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)[DB_NAME]

    for run_id in run_ids:
        print("\n" + "=" * 72)
        print(f"  RUN {run_id}")
        print("=" * 72)
        try:
            meta = fetch_run(run_id)
            print(
                f"  window: {meta.get('date_from')} -> {meta.get('date_to')}  "
                f"status={meta.get('status')}  msg={str(meta.get('message') or '')[:70]}"
            )
        except Exception as exc:
            print(f"  api error: {exc}")

        closes = list(
            db.strategy_positions_historical.find(
                {"run_id": run_id, "event": "POSITION_CLOSE"},
                {"trade_date_ist": 1, "direction": 1, "entry_strategy": 1, "_id": 0},
            )
        )
        by_month: Counter[str] = Counter()
        by_day: Counter[str] = Counter()
        by_dir: Counter[str] = Counter()
        by_strat: Counter[str] = Counter()
        for doc in closes:
            d = str(doc.get("trade_date_ist") or "")[:10]
            by_day[d] += 1
            by_month[d[:7]] += 1
            by_dir[str(doc.get("direction") or "?")] += 1
            by_strat[str(doc.get("entry_strategy") or "?")] += 1

        print(f"\n  closes: {len(closes)}")
        print("  by month:", dict(sorted(by_month.items())))
        if by_day:
            days = sorted(by_day.keys())
            print(f"  trade days: {days[0]} .. {days[-1]}  ({len(by_day)} days)")
        print("  by direction:", dict(by_dir))
        print("  by entry_strategy:", dict(by_strat))

        ml_votes = db.strategy_votes_historical.count_documents(
            {"run_id": run_id, "strategy": "ML_ENTRY", "signal_type": "ENTRY"}
        )
        ml_ge = db.strategy_votes_historical.count_documents(
            {
                "run_id": run_id,
                "strategy": "ML_ENTRY",
                "signal_type": "ENTRY",
                "confidence": {"$gte": 0.65},
            }
        )
        print(f"\n  ML_ENTRY votes: {ml_votes}  (>=0.65: {ml_ge})")

        blockers: Counter[str] = Counter()
        for doc in db.strategy_decision_traces_historical.find(
            {"run_id": run_id, "final_outcome": "blocked"},
            {"primary_blocker_gate": 1},
        ):
            gate = doc.get("primary_blocker_gate")
            if isinstance(gate, dict):
                label = str(gate.get("gate_id") or gate.get("reason_code") or "unknown")
            else:
                label = str(gate or "unknown")
            blockers[label] += 1
        if blockers:
            print("  top blockers:")
            for gate, cnt in blockers.most_common(8):
                print(f"    {gate:<40} {cnt}")

        traces_by_month: dict[str, int] = defaultdict(int)
        for doc in db.strategy_decision_traces_historical.find(
            {"run_id": run_id},
            {"trade_date_ist": 1},
        ):
            td = str(doc.get("trade_date_ist") or doc.get("timestamp") or "")[:7]
            if td:
                traces_by_month[td] += 1
        if traces_by_month:
            print("  decision traces by month:", dict(sorted(traces_by_month.items())))

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
