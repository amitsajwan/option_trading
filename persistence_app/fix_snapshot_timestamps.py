from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from .time_utils import minute_bucket_ist, parse_market_timestamp_ist, to_ist

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def _load_env() -> None:
    if load_dotenv is None:
        return
    runtime_file = Path(__file__).resolve()
    repo_root = next(
        (parent for parent in runtime_file.parents if (parent / "market_data" / "src").exists()),
        runtime_file.parents[1],
    )
    candidates = [
        Path.cwd() / ".env",
        repo_root / ".env",
        repo_root / "market_data" / ".env",
    ]
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            load_dotenv(path, override=False)


def _mongo_client() -> MongoClient:
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        return MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=5000)
    return MongoClient(
        host=str(os.getenv("MONGO_HOST") or "localhost"),
        port=int(os.getenv("MONGO_PORT") or "27017"),
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=5000,
    )


def run(*, dry_run: bool) -> int:
    _load_env()
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    coll_name = str(os.getenv("MONGO_COLL_SNAPSHOTS") or "phase1_market_snapshots").strip() or "phase1_market_snapshots"

    client = _mongo_client()
    client.admin.command("ping")
    coll = client[db_name][coll_name]

    query = {
        "event_type": "snapshot",
        "payload.snapshot.session_context.timestamp": {"$type": "string"},
    }
    projection: dict[str, Any] = {
        "_id": 1,
        "timestamp": 1,
        "trade_date_ist": 1,
        "market_time_ist": 1,
        "market_minute": 1,
        "payload.snapshot.session_context.timestamp": 1,
    }

    scanned = 0
    updated = 0
    skipped = 0

    for doc in coll.find(query, projection=projection):
        scanned += 1
        raw_ts = (
            doc.get("payload", {})
            .get("snapshot", {})
            .get("session_context", {})
            .get("timestamp")
        )
        ts_ist = parse_market_timestamp_ist(raw_ts)
        if ts_ist is None:
            skipped += 1
            continue
        ts_ist = to_ist(ts_ist)
        new_trade_date = ts_ist.date().isoformat()
        new_market_time = ts_ist.strftime("%H:%M:%S")

        changed = (
            doc.get("timestamp") != ts_ist
            or doc.get("trade_date_ist") != new_trade_date
            or doc.get("market_time_ist") != new_market_time
        )
        if not changed:
            continue

        update_fields: dict[str, Any] = {
            "timestamp": ts_ist,
            "trade_date_ist": new_trade_date,
            "market_time_ist": new_market_time,
        }
        if "market_minute" in doc:
            update_fields["market_minute"] = minute_bucket_ist(ts_ist)

        if not dry_run:
            coll.update_one({"_id": doc["_id"]}, {"$set": update_fields})
        updated += 1

    mode = "DRY-RUN" if dry_run else "APPLIED"
    print(
        f"[{mode}] db={db_name} coll={coll_name} scanned={scanned} updated={updated} skipped={skipped}"
    )
    return 0


def run_cli() -> int:
    parser = argparse.ArgumentParser(description="Fix IST timestamp skew in persisted snapshot docs.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not write updates")
    args = parser.parse_args()
    return run(dry_run=bool(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(run_cli())
