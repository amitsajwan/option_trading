#!/usr/bin/env python3
"""E3-S3: Direction quality analysis — compare CE/PE PF by direction_source.

Joins closed positions with the winning ML_ENTRY vote to extract
direction_source (momentum | direction_ml | pe_only | …) and shows
whether the direction ML model improved CE PF over the momentum baseline.

Usage inside dashboard container:
    python /tmp/analyze_direction_quality.py <RUN_ID> [<label>]

Or from VM host (copies itself in):
    sudo docker cp ops/gcp/analyze_direction_quality.py option_trading-dashboard-1:/tmp/
    sudo docker exec option_trading-dashboard-1 \
        python /tmp/analyze_direction_quality.py <RUN_ID> direction_ml
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import defaultdict
from statistics import mean

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo required", file=sys.stderr)
    sys.exit(2)

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
DB_NAME = os.environ.get("MONGO_DB", "trading_ai")
LOT_SIZE = 15
CAPITAL = 100_000


def _latest_run_id() -> str:
    with urllib.request.urlopen(
        "http://127.0.0.1:8008/api/strategy/evaluation/runs/latest?dataset=historical",
        timeout=15,
    ) as r:
        d = json.load(r)
    run = d if isinstance(d, dict) and d.get("run_id") else d.get("run") or d
    rid = str(run.get("run_id") or "").strip()
    if not rid:
        raise SystemExit("Could not resolve latest run_id")
    return rid


def _profit_factor(pnls: list[float]) -> float:
    wins = sum(p for p in pnls if p > 0)
    loss = abs(sum(p for p in pnls if p <= 0))
    return wins / loss if loss > 0 else float("inf")


def _win_rate(pnls: list[float]) -> float:
    return sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0


def main() -> int:
    argv = [a for a in sys.argv[1:] if a]
    run_id = argv[0] if argv and not argv[0].startswith("-") else ""
    label = argv[1] if len(argv) > 1 else os.environ.get("OOS_LABEL", "oos")

    if not run_id:
        run_id = _latest_run_id()

    db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)[DB_NAME]

    # Load closed positions
    closes = list(
        db.strategy_positions_historical.find(
            {"run_id": run_id, "event": "POSITION_CLOSE"},
            {"_id": 0},
        )
    )
    if not closes:
        print(f"No closed positions for run_id={run_id}")
        return 1

    # Build snapshot_id → ML_ENTRY vote map for direction_source
    entry_snap_ids = {
        str(c.get("entry_snapshot_id") or "")
        for c in closes
        if c.get("entry_snapshot_id")
    }
    snap_to_direction_source: dict[str, str] = {}
    for vote in db.strategy_votes_historical.find(
        {
            "run_id": run_id,
            "strategy": "ML_ENTRY",
            "signal_type": "ENTRY",
            "snapshot_id": {"$in": list(entry_snap_ids)},
        },
        {"snapshot_id": 1, "raw_signals": 1, "_id": 0},
    ):
        sid = str(vote.get("snapshot_id") or "")
        raw = vote.get("raw_signals") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        src = str(raw.get("direction_source") or "unknown")
        snap_to_direction_source[sid] = src

    print("\n" + "=" * 72)
    print(f"  DIRECTION QUALITY — {label}")
    print(f"  run_id: {run_id}   positions: {len(closes)}")
    print("=" * 72)

    # Group trades by (direction_source, direction) for PF comparison
    by_src_dir: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_src: dict[str, list[float]] = defaultdict(list)
    no_source = 0
    for c in closes:
        pnl = float(c.get("pnl_pct") or 0)
        direction = str(c.get("direction") or "?")
        sid = str(c.get("entry_snapshot_id") or "")
        src = snap_to_direction_source.get(sid, "unknown")
        if src == "unknown":
            no_source += 1
        by_src_dir[(src, direction)].append(pnl)
        by_src[src].append(pnl)

    if no_source:
        print(f"\n  Note: {no_source}/{len(closes)} trades had no matching ML_ENTRY vote → unknown source")

    # Overall by direction_source
    print("\n  ── By direction_source (all directions) ──")
    print(f"  {'source':<25} {'n':>4}  {'PF':>6}  {'WR':>6}  {'avg%':>8}")
    for src in sorted(by_src.keys()):
        pnls = by_src[src]
        pf = _profit_factor(pnls)
        wr = _win_rate(pnls)
        avg = mean(pnls) * 100
        print(f"  {src:<25} {len(pnls):>4}  {pf:>6.2f}  {wr*100:>5.0f}%  {avg:>+7.2f}%")

    # CE / PE split by direction_source
    print("\n  ── CE leg by direction_source ──")
    print(f"  {'source':<25} {'n':>4}  {'CE_PF':>7}  {'CE_WR':>7}  {'CE_avg%':>9}")
    for src in sorted(by_src.keys()):
        pnls = by_src_dir.get((src, "CE"), [])
        if not pnls:
            print(f"  {src:<25}    0        —        —          —")
            continue
        pf = _profit_factor(pnls)
        wr = _win_rate(pnls)
        avg = mean(pnls) * 100
        print(f"  {src:<25} {len(pnls):>4}  {pf:>7.2f}  {wr*100:>6.0f}%  {avg:>+8.2f}%")

    print("\n  ── PE leg by direction_source ──")
    print(f"  {'source':<25} {'n':>4}  {'PE_PF':>7}  {'PE_WR':>7}  {'PE_avg%':>9}")
    for src in sorted(by_src.keys()):
        pnls = by_src_dir.get((src, "PE"), [])
        if not pnls:
            print(f"  {src:<25}    0        —        —          —")
            continue
        pf = _profit_factor(pnls)
        wr = _win_rate(pnls)
        avg = mean(pnls) * 100
        print(f"  {src:<25} {len(pnls):>4}  {pf:>7.2f}  {wr*100:>6.0f}%  {avg:>+8.2f}%")

    # E3-S3 publish gate: CE PF >= 1.0 for direction_ml source
    print("\n  ── E3-S3 direction_ml gate ──")
    ml_ce = by_src_dir.get(("direction_ml", "CE"), [])
    ml_pe = by_src_dir.get(("direction_ml", "PE"), [])
    ml_all = by_src.get("direction_ml", [])
    if not ml_all:
        print("  No direction_ml trades found — direction ML was not active in this run")
        print("  (run with DIRECTION_ML_MODEL_PATH set to use direction_ml source)")
        return 0

    ml_ce_pf = _profit_factor(ml_ce) if ml_ce else None
    ml_pe_pf = _profit_factor(ml_pe) if ml_pe else None
    ml_pf = _profit_factor(ml_all)

    ce_ok = ml_ce_pf is not None and ml_ce_pf >= 1.0
    pe_ok = ml_pe_pf is not None and ml_pe_pf >= 1.0
    pf_ok = ml_pf >= 1.30
    n_ok = len(ml_all) >= 25

    checks = [
        ("direction_ml CE PF ≥ 1.00", ce_ok, "n/a" if ml_ce_pf is None else f"{ml_ce_pf:.2f}"),
        ("direction_ml PE PF ≥ 1.00", pe_ok, "n/a" if ml_pe_pf is None else f"{ml_pe_pf:.2f}"),
        ("direction_ml portfolio PF ≥ 1.30", pf_ok, f"{ml_pf:.2f}"),
        ("direction_ml trades ≥ 25", n_ok, str(len(ml_all))),
    ]
    all_ok = True
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"    [{mark}] {name:<38}  ({detail})")
    print("\n  " + ("DIRECTION ML: PASS → proceed to OOS validation" if all_ok else "DIRECTION ML: FAIL → investigate CE leg"))

    # Compare vs momentum baseline
    mom_ce = by_src_dir.get(("momentum", "CE"), [])
    if mom_ce and ml_ce:
        delta = _profit_factor(ml_ce) - _profit_factor(mom_ce)
        print(f"\n  CE PF delta vs momentum: {delta:+.2f}")
        print(f"    momentum CE PF={_profit_factor(mom_ce):.2f} (n={len(mom_ce)})")
        print(f"    direction_ml CE PF={_profit_factor(ml_ce):.2f} (n={len(ml_ce)})")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
