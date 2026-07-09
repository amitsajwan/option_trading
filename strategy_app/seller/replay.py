"""Seller replay harness (Phase 1 of docs/SELLER_V2_PLAN.md, 2026-07-10).

Drives the REAL SellerRunner over recorded chain snapshots — the same
on_snapshot() code path the live daemon polls, with the paper gateway filling
from the recorded chain. NOT a parallel implementation: the 2024 seller
backtest ran on a separate harness while the live path hid a bug that made
real entries impossible (2099-expiry). Never again — this harness IS the
live code with a recorded feed.

Usage (inside any container with strategy_app + pymongo, e.g. seller_app):
  python -m strategy_app.seller.replay --from 2026-06-01 --to 2026-07-09 \
      [--coll phase1_market_snapshots] [--out /tmp/seller_replay]

Multi-day holds work: ONE runner instance consumes every snapshot in
chronological order; daily gates reset on real date rollovers exactly as live.
Results: per-trade records + summary JSON in --out.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime


class _ReplayColl:
    """Absorbs the runner's status/position mirrors without touching live data."""

    def __init__(self, sink: list):
        self._sink = sink

    def update_one(self, *a, **k):
        pass

    def insert_one(self, doc):
        self._sink.append(doc)

    def delete_one(self, *a, **k):
        pass

    def find_one(self, *a, **k):
        # bn-conflict guard: replay has no live buy-side book -> no conflict
        return None


class _ReplayDb:
    def __init__(self):
        self.trades: list = []
        self.positions: list = []

    def __getitem__(self, name):
        if name == "seller_trades":
            return _ReplayColl(self.trades)
        if name == "seller_positions":
            return _ReplayColl(self.positions)
        return _ReplayColl([])


def _snapshots(mongo_db, coll: str, d_from: str, d_to: str):
    """Recorded snapshots in strict chronological order (the live feed, replayed)."""
    cur = mongo_db[coll].find(
        {"trade_date_ist": {"$gte": d_from, "$lte": d_to}},
        sort=[("timestamp", 1)],
    )
    for doc in cur:
        snap = (doc.get("payload") or {}).get("snapshot")
        if snap:
            yield snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--coll", default="phase1_market_snapshots")
    ap.add_argument("--out", default="/tmp/seller_replay")
    ap.add_argument("--label", default="baseline")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    run_dir = os.path.join(args.out, f"{args.label}_{args.d_from}_{args.d_to}")
    os.makedirs(run_dir, exist_ok=True)
    # Isolate the runner's durable store + trade log into this run's dir, and be
    # explicit that this is NEVER live: paper gateway only, alerts impossible.
    os.environ["STRATEGY_RUN_DIR"] = run_dir
    os.environ["SHARED_RUN_DIR"] = run_dir          # keep the priority flag out of live .run
    os.environ["SELLER_LIVE_ENABLED"] = "0"
    os.environ.setdefault("SELLER_MIN_FREE_MARGIN_RS", "0")   # paper: no margin gate

    from pymongo import MongoClient
    from strategy_app.seller.runner import SellerRunner

    live_db = MongoClient(os.getenv("MONGO_HOST", "mongo"),
                          int(os.getenv("MONGO_PORT", "27017") or 27017))[
        os.getenv("MONGO_DB", "trading_ai")]
    replay_db = _ReplayDb()
    runner = SellerRunner(replay_db)
    assert runner._mode == "paper", "replay must never construct a live gateway"

    n = 0
    for snap in _snapshots(live_db, args.coll, args.d_from, args.d_to):
        runner.on_snapshot(snap)
        n += 1

    # Summarise from the runner's own trade log (the audit trail live writes).
    closes, opens = [], 0
    log_path = os.path.join(run_dir, "seller_trades.jsonl")
    if os.path.exists(log_path):
        for ln in open(log_path):
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "open":
                opens += 1
            elif rec.get("event") == "close":
                closes.append(rec)

    pnls = [float(c.get("pnl_rs") or 0) for c in closes]
    reasons = Counter(c.get("reason") for c in closes)
    summary = {
        "label": args.label, "from": args.d_from, "to": args.d_to,
        "snapshots": n, "opens": opens, "closes": len(closes),
        "still_open": opens - len(closes),
        "wins": sum(1 for p in pnls if p > 0),
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3) if pnls else None,
        "total_pnl_rs": round(sum(pnls)),
        "avg_pnl_rs": round(sum(pnls) / len(pnls)) if pnls else None,
        "worst_rs": round(min(pnls)) if pnls else None,
        "best_rs": round(max(pnls)) if pnls else None,
        "exit_reasons": dict(reasons),
        "generated_at": datetime.utcnow().isoformat(),
    }
    with open(os.path.join(run_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
