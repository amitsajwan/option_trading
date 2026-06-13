"""Create the indexes the historical-replay dashboard endpoint depends on.

Idempotent migration. The ``GET /api/historical/replay/dates`` endpoint runs a
``distinct`` plus an N+1 query loop over up to ~250 dates against the
``*_historical`` collections. Without these indexes those queries full-scan
``strategy_positions_historical`` (150k+ docs) ~1000x and blow past mongo's 5s
socket timeout -> the UI shows ``Failed to load dates: HTTP 500``.

These indexes are NOT created by the persistence layer (which only indexes the
LIVE collections) and are lost on a data-only ``mongorestore`` — hence this
migration, so a future restore can re-apply them in one command.

Usage:
    python -m ops.migrations.historical_replay_indexes_init            # apply
    python -m ops.migrations.historical_replay_indexes_init --dry-run  # report

Connection params come from MONGO_URI / MONGO_HOST / MONGO_PORT / MONGO_DB
(same as the rest of the live stack).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("ops.migrations.historical_replay_indexes_init")

ASCENDING = 1

# (collection-name resolver env var, default name, [index key specs])
# Index key specs are lists of (field, direction) tuples.
_INDEX_PLAN: List[Tuple[str, str, List[List[Tuple[str, int]]]]] = [
    (
        "MONGO_COLL_SNAPSHOTS_HISTORICAL",
        "phase1_market_snapshots_historical",
        [[("trade_date_ist", ASCENDING)]],
    ),
    (
        "MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL",
        "strategy_positions_historical",
        [
            [("trade_date_ist", ASCENDING), ("run_id", ASCENDING), ("event", ASCENDING)],
            [("trade_date_ist", ASCENDING), ("run_id", ASCENDING)],
        ],
    ),
    (
        "MONGO_COLL_STRATEGY_EVAL_RUNS",
        "strategy_eval_runs",
        [[("trade_date_ist", ASCENDING), ("status", ASCENDING)]],
    ),
    (
        "MONGO_COLL_TRADE_SIGNALS_HISTORICAL",
        "trade_signals_historical",
        [[("trade_date_ist", ASCENDING)]],
    ),
    (
        "MONGO_COLL_STRATEGY_VOTES_HISTORICAL",
        "strategy_votes_historical",
        [[("trade_date_ist", ASCENDING)]],
    ),
]


def _connect_mongo_db() -> Any:
    from pymongo import MongoClient  # local import keeps unit tests light

    uri = os.getenv("MONGO_URI") or (
        f"mongodb://{os.getenv('MONGO_HOST', 'localhost')}:{os.getenv('MONGO_PORT', '27017')}"
    )
    db_name = os.getenv("MONGO_DB", "trading_ai")
    return MongoClient(uri)[db_name]


def apply_migration(db: Any, *, dry_run: bool = False) -> dict[str, Any]:
    created: list[str] = []
    existing: list[str] = []
    skipped: list[str] = []

    for env_var, default_name, specs in _INDEX_PLAN:
        coll_name = os.getenv(env_var) or default_name
        coll = db[coll_name]
        try:
            info = coll.index_information()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("index_information(%s) failed: %s", coll_name, exc)
            info = {}
        existing_keys = {tuple(v.get("key", [])) for v in info.values()}

        for spec in specs:
            key_tuple = tuple((f, d) for f, d in spec)
            label = f"{coll_name}:{'+'.join(f for f, _ in spec)}"
            if key_tuple in existing_keys:
                existing.append(label)
                continue
            if dry_run:
                skipped.append(f"WOULD create {label}")
                continue
            try:
                name = coll.create_index(spec)
                created.append(f"{label} ({name})")
            except Exception as exc:
                logger.warning("create_index(%s) failed: %s", label, exc)
                skipped.append(f"FAILED {label}: {exc}")

    return {
        "dry_run": dry_run,
        "created": created,
        "existing": existing,
        "skipped": skipped,
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Report without writing.")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db = _connect_mongo_db()
    result = apply_migration(db, dry_run=bool(args.dry_run))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
