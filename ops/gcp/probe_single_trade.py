#!/usr/bin/env python3
"""Single-trade postmortem probe: dump everything about one position.

Pulls the position, its ML_ENTRY vote (raw_signals with direction scores/probs),
the decision trace, and surrounding snapshots so we can answer: what signals were
available at entry, and why was this direction chosen?

Usage (inside dashboard container):
  python /tmp/probe_single_trade.py --pos fce59da2
  python /tmp/probe_single_trade.py --pos fce59da2 --db trading_ai
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo required", file=sys.stderr)
    sys.exit(2)


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonable(v) for v in o]
    if isinstance(o, datetime):
        return o.isoformat()
    try:
        json.dumps(o)
        return o
    except (TypeError, ValueError):
        return str(o)


def _dump(label, doc):
    print(f"\n===== {label} =====")
    if doc is None:
        print("  (none found)")
        return
    print(json.dumps(_jsonable(doc), indent=2, default=str))


def _find_prefix(coll, field, prefix):
    """Find a doc whose `field` starts with prefix (8-char short id)."""
    return coll.find_one({field: {"$regex": f"^{prefix}"}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos", required=True, help="position id (full or 8-char prefix)")
    ap.add_argument("--db", default=os.getenv("MONGO_DB", "trading_ai"))
    ap.add_argument("--url", default=os.getenv("MONGO_URL", "mongodb://mongo:27017"))
    args = ap.parse_args()

    db = MongoClient(args.url)[args.db]
    print(f"DB={args.db} collections={db.list_collection_names()}")

    pfx = args.pos

    def search(coll_name, fields):
        if coll_name not in db.list_collection_names():
            return None, None
        coll = db[coll_name]
        for fld in fields:
            d = _find_prefix(coll, fld, pfx)
            if d:
                return d, fld
        return None, None

    # 1) Position
    pos, fld = search("strategy_positions", ("position_id", "id", "trade_id", "signal_id"))
    if pos:
        print(f"\n[matched position in strategy_positions on field '{fld}']")
    _dump("POSITION", pos)

    full_id = None
    entry_ts = None
    if pos:
        full_id = pos.get("position_id") or pos.get("id") or pos.get("trade_id")
        entry_ts = pos.get("entry_time") or pos.get("opened_at") or pos.get("entry_ts")
    print(f"\n[full_id={full_id} entry_ts={entry_ts}]")

    # 2) Trade signal (carries vote + raw_signals + direction scores)
    sig = None
    for sc in ("trade_signals", "strategy_votes"):
        sig, sfld = search(sc, ("signal_id", "position_id", "id", "trade_id"))
        if sig:
            print(f"\n[matched signal in {sc} on '{sfld}']")
            _dump(f"SIGNAL[{sc}]", sig)
            break

    # 3) Votes around this position id (look in strategy_votes broadly)
    if "strategy_votes" in db.list_collection_names() and full_id:
        cur = list(db["strategy_votes"].find({"$or": [
            {"position_id": full_id},
            {"position_id": {"$regex": f"^{pfx}"}},
            {"signal_id": {"$regex": f"^{pfx}"}},
        ]}).limit(10))
        print(f"\n[strategy_votes matched {len(cur)} docs by id]")
        for i, v in enumerate(cur):
            _dump(f"VOTE[{i}]", v)

    # 4) Decision traces + pipeline events
    for tracecoll in ("strategy_decision_traces", "pipeline_decision_events"):
        t, tfld = search(tracecoll, ("position_id", "signal_id", "id", "trace_id"))
        if t:
            print(f"\n[matched trace in {tracecoll} on '{tfld}']")
        _dump(f"TRACE[{tracecoll}]", t)


if __name__ == "__main__":
    main()
