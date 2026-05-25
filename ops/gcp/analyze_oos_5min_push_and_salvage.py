#!/usr/bin/env python3
"""
Compute two diagnostics on a completed historical replay run:
1) 5-minute underlying (BN futures) push availability: count of trades with
   max(5m_up_pts, 5m_down_pts) >= threshold (default 60 pts), overall and
   broken down by entry time-of-day buckets (30-min IST windows).
2) TIME_STOP salvage opportunity: among TIME_STOP trades, how many had interim
   MFE >= 3% and >= 5% (option PnL) at any time before exit.

Inputs:
- CSV produced by ops/gcp/analyze_trade_forensics.py (fields include
  up_pts_5m, down_pts_5m, exit_reason, mfe_pct, position_id)
- MongoDB (to map position_id -> entry time-of-day bucket via POSITION_OPEN doc)

Usage (inside dashboard container):
  python /tmp/analyze_oos_5min_push_and_salvage.py \
    --run-id <run_id> \
    --csv /tmp/e7_forensics.csv \
    --pts-threshold 60
"""
from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict
from typing import Dict, List, Tuple

try:
    from pymongo import ASCENDING, MongoClient
except ImportError:
    MongoClient = None  # type: ignore


def time_bucket_from_hhmm(hhmmss: str) -> str:
    if not hhmmss or len(hhmmss) < 5:
        return "unknown"
    try:
        hh = int(hhmmss[:2])
        mm = int(hhmmss[3:5])
    except ValueError:
        return "unknown"
    mins = hh * 60 + mm
    start = 9 * 60 + 15
    end = 15 * 60 + 30
    if mins < start:
        return "pre_open"
    if mins >= end:
        return "post_close"
    idx = (mins - start) // 30
    bstart = start + idx * 30
    bend = bstart + 30
    return f"{bstart//60:02d}:{bstart%60:02d}-{bend//60:02d}:{bend%60:02d}"


def load_open_buckets(db, run_id: str) -> Dict[str, str]:
    """Return position_id -> time bucket at entry (POSITION_OPEN doc)."""
    by_pos: Dict[str, str] = {}
    cur = db.strategy_positions_historical.find(
        {"run_id": run_id, "event": "POSITION_OPEN"},
        {"position_id": 1, "market_time_ist": 1},
    ).sort("timestamp", ASCENDING)
    for doc in cur:
        pid = str(doc.get("position_id") or "").strip()
        if not pid:
            continue
        mt = str(doc.get("market_time_ist") or "")
        by_pos[pid] = time_bucket_from_hhmm(mt)
    return by_pos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--pts-threshold", type=float, default=60.0)
    args = ap.parse_args()

    # Load CSV rows
    rows: List[dict] = []
    with open(args.csv, newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        for row in r:
            rows.append(row)

    n = len(rows)
    pts_ok = sum(
        1
        for row in rows
        if float(row.get("up_pts_5m") or 0) >= args.pts_threshold
        or float(row.get("down_pts_5m") or 0) >= args.pts_threshold
    )

    # Mongo for entry-time buckets
    url = os.getenv("MONGO_URL", "mongodb://mongo:27017")
    dbname = os.getenv("MONGO_DB", "trading_ai")
    db = MongoClient(url, serverSelectionTimeoutMS=8000)[dbname] if MongoClient else None
    buckets: Dict[str, str] = {}
    if db is not None:
        buckets = load_open_buckets(db, args.run_id)

    by_bucket_total: Dict[str, int] = defaultdict(int)
    by_bucket_ptsok: Dict[str, int] = defaultdict(int)
    for row in rows:
        pid = str(row.get("position_id") or "")
        b = buckets.get(pid, "unknown")
        by_bucket_total[b] += 1
        up = float(row.get("up_pts_5m") or 0)
        dn = float(row.get("down_pts_5m") or 0)
        if up >= args.pts_threshold or dn >= args.pts_threshold:
            by_bucket_ptsok[b] += 1

    # TIME_STOP salvage
    ts_rows = [r for r in rows if (r.get("exit_reason") or "").upper() == "TIME_STOP"]
    ts_n = len(ts_rows)
    salvage_3 = sum(1 for r in ts_rows if float(r.get("mfe_pct") or 0) >= 0.03)
    salvage_5 = sum(1 for r in ts_rows if float(r.get("mfe_pct") or 0) >= 0.05)

    print("\n==== 5-minute push availability ====")
    print(f"run_id: {args.run_id}")
    print(f"threshold: >= {args.pts_threshold:.0f} pts")
    print(f"overall: {pts_ok}/{n} ({(100.0*pts_ok/max(1,n)):.1f}%)")

    print("\n-- by entry time bucket (30-min IST) --")
    for b in sorted(by_bucket_total.keys()):
        t = by_bucket_total[b]
        k = by_bucket_ptsok.get(b, 0)
        pct = 100.0 * k / t if t else 0.0
        print(f"{b:>11}: {k}/{t} ({pct:.1f}%)")

    print("\n==== TIME_STOP salvage opportunity ====")
    print(f"TIME_STOP trades: {ts_n}")
    print(f"had MFE >= +3%: {salvage_3}/{ts_n} ({(100.0*salvage_3/max(1,ts_n)):.1f}%)")
    print(f"had MFE >= +5%: {salvage_5}/{ts_n} ({(100.0*salvage_5/max(1,ts_n)):.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
