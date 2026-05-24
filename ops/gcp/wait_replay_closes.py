#!/usr/bin/env python3
"""Wait until historical replay positions have drained into Mongo."""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo required", file=sys.stderr)
    sys.exit(2)


def _count_closes(db, run_id: str) -> int:
    return db.strategy_positions_historical.count_documents(
        {"run_id": run_id, "event": "POSITION_CLOSE"}
    )


def _run_status(run_id: str) -> str:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:8008/api/strategy/evaluation/runs/{run_id}",
            timeout=15,
        ) as resp:
            import json

            return str(json.load(resp).get("status") or "").lower()
    except Exception:
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--min-closes", type=int, default=400)
    parser.add_argument("--stable-polls", type=int, default=6, help="30s polls with unchanged count")
    parser.add_argument("--poll-sec", type=int, default=30)
    parser.add_argument("--timeout-sec", type=int, default=3600)
    args = parser.parse_args()

    url = os.getenv("MONGO_URL", "mongodb://mongo:27017")
    db_name = os.getenv("MONGO_DB", "trading_ai")
    db = MongoClient(url, serverSelectionTimeoutMS=10000)[db_name]

    rid = args.run_id
    deadline = time.time() + args.timeout_sec
    last = -1
    stable = 0
    poll = 0

    while time.time() < deadline:
        poll += 1
        n = _count_closes(db, rid)
        status = _run_status(rid)
        print(f"poll={poll} status={status} closes={n}", flush=True)

        if n >= args.min_closes and n == last:
            stable += 1
            if stable >= args.stable_polls:
                print(f"ready closes={n} (stable {stable} polls)", flush=True)
                return 0
        else:
            stable = 0

        last = n
        if status in {"failed", "cancelled"}:
            print(f"run ended status={status} closes={n}", flush=True)
            return 1 if n < args.min_closes else 0

        time.sleep(args.poll_sec)

    print(f"timeout closes={last} min={args.min_closes}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
