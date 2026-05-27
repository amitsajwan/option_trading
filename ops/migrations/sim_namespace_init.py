"""SIM-2 — initialize Mongo schema for the sim namespace.

Idempotent migration. Creates the ``*_sim`` collections that mirror the
live collections, with:

  * a TTL index on ``created_at`` (default 30d) so sim experiments
    self-clean.
  * a compound ``(run_id, created_at)`` index for fast per-run queries.

Also adds a ``kind`` field to the shared ``strategy_eval_runs`` registry:

  * existing rows that lack it are stamped ``"oos"`` (the registry was
    historically used for OOS validation runs only).
  * an index on ``(kind, created_at)`` so "list all sim runs" queries are
    cheap.

Usage:

    python -m ops.migrations.sim_namespace_init             # apply
    python -m ops.migrations.sim_namespace_init --dry-run   # report only

Connection params come from MONGO_URI / MONGO_HOST / MONGO_PORT /
MONGO_DB env vars (same as the rest of the live stack).

See:
    docs/SCRUM_BOARD_SIM_REPLAY.md  (SIM-2)
    memory/project_sim_replay_design_2026-05-27  (design doc)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from contracts_app import resolve_namespace

logger = logging.getLogger("ops.migrations.sim_namespace_init")

DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 days

# Bases that get a parallel ``*_sim`` collection. Names resolved via
# ``resolve_namespace`` so any rename of the namespace pattern is a
# single-file change.
_NAMESPACED_BASES: tuple[str, ...] = (
    "phase1_market_snapshots",
    "strategy_votes",
    "trade_signals",
    "strategy_positions",
    "strategy_decision_traces",
    "market_depth_ticks",
)

_REGISTRY_COLL = "strategy_eval_runs"


# ── result accumulators ───────────────────────────────────────────────────


@dataclass
class MigrationResult:
    """Summary of what the migration did (or would have done in dry-run)."""

    dry_run: bool
    sim_collections_created: list[str] = field(default_factory=list)
    sim_collections_existing: list[str] = field(default_factory=list)
    ttl_indexes_created: list[str] = field(default_factory=list)
    ttl_indexes_existing: list[str] = field(default_factory=list)
    compound_indexes_created: list[str] = field(default_factory=list)
    compound_indexes_existing: list[str] = field(default_factory=list)
    registry_rows_stamped: int = 0
    registry_index_created: bool = False
    registry_index_existing: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "sim_collections_created": list(self.sim_collections_created),
            "sim_collections_existing": list(self.sim_collections_existing),
            "ttl_indexes_created": list(self.ttl_indexes_created),
            "ttl_indexes_existing": list(self.ttl_indexes_existing),
            "compound_indexes_created": list(self.compound_indexes_created),
            "compound_indexes_existing": list(self.compound_indexes_existing),
            "registry_rows_stamped": int(self.registry_rows_stamped),
            "registry_index_created": bool(self.registry_index_created),
            "registry_index_existing": bool(self.registry_index_existing),
            "notes": list(self.notes),
        }


# ── core ──────────────────────────────────────────────────────────────────


def _existing_collection_names(db: Any) -> set[str]:
    try:
        return set(db.list_collection_names())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("list_collection_names failed: %s", exc)
        return set()


def _index_named(collection: Any, name: str) -> bool:
    try:
        info = collection.index_information()
    except Exception:  # pragma: no cover - defensive
        return False
    return name in info


def _ensure_sim_collection(
    db: Any,
    base: str,
    *,
    existing_names: set[str],
    ttl_seconds: int,
    dry_run: bool,
    result: MigrationResult,
) -> None:
    coll_name = resolve_namespace("sim", run_id="placeholder").collection_for(base)
    coll = db[coll_name]
    # Collection
    if coll_name in existing_names:
        result.sim_collections_existing.append(coll_name)
    else:
        if dry_run:
            result.notes.append(f"WOULD create collection {coll_name}")
        else:
            db.create_collection(coll_name)
            logger.info("created collection %s", coll_name)
        result.sim_collections_created.append(coll_name)
    # TTL index on created_at
    ttl_name = "ttl_created_at"
    if _index_named(coll, ttl_name):
        result.ttl_indexes_existing.append(coll_name)
    else:
        if dry_run:
            result.notes.append(
                f"WOULD create TTL index {ttl_name} (expireAfterSeconds={ttl_seconds}) on {coll_name}"
            )
        else:
            coll.create_index(
                [("created_at", 1)],
                name=ttl_name,
                expireAfterSeconds=ttl_seconds,
            )
            logger.info(
                "created TTL index on %s (expireAfterSeconds=%d)", coll_name, ttl_seconds
            )
        result.ttl_indexes_created.append(coll_name)
    # Compound (run_id, created_at) for per-run queries
    compound_name = "run_id_created_at"
    if _index_named(coll, compound_name):
        result.compound_indexes_existing.append(coll_name)
    else:
        if dry_run:
            result.notes.append(
                f"WOULD create compound index {compound_name} on {coll_name}"
            )
        else:
            coll.create_index(
                [("run_id", 1), ("created_at", 1)],
                name=compound_name,
            )
            logger.info("created compound index on %s", coll_name)
        result.compound_indexes_created.append(coll_name)


def _stamp_registry_kind(
    db: Any,
    *,
    default_kind: str,
    dry_run: bool,
    result: MigrationResult,
) -> None:
    coll = db[_REGISTRY_COLL]
    try:
        unset_count = coll.count_documents({"kind": {"$exists": False}})
    except Exception as exc:  # pragma: no cover - defensive
        result.notes.append(f"could not count un-kinded registry rows: {exc}")
        unset_count = 0
    if unset_count > 0:
        if dry_run:
            result.notes.append(
                f"WOULD stamp {unset_count} rows in {_REGISTRY_COLL} with kind={default_kind!r}"
            )
        else:
            res = coll.update_many(
                {"kind": {"$exists": False}},
                {"$set": {"kind": default_kind}},
            )
            modified = getattr(res, "modified_count", unset_count)
            logger.info(
                "stamped %d existing %s rows with kind=%s",
                modified,
                _REGISTRY_COLL,
                default_kind,
            )
            result.registry_rows_stamped = int(modified)
    # Index on (kind, created_at)
    kind_idx_name = "kind_created_at"
    if _index_named(coll, kind_idx_name):
        result.registry_index_existing = True
    else:
        if dry_run:
            result.notes.append(
                f"WOULD create index {kind_idx_name} on {_REGISTRY_COLL}"
            )
        else:
            coll.create_index(
                [("kind", 1), ("created_at", 1)],
                name=kind_idx_name,
            )
            logger.info("created kind/created_at index on %s", _REGISTRY_COLL)
        result.registry_index_created = True


def apply_migration(
    db: Any,
    *,
    bases: Iterable[str] = _NAMESPACED_BASES,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    default_registry_kind: str = "oos",
    dry_run: bool = False,
) -> MigrationResult:
    """Run the migration against a connected pymongo Database.

    Pass ``dry_run=True`` to log what *would* change without writing.
    """
    result = MigrationResult(dry_run=dry_run)
    existing_names = _existing_collection_names(db)
    for base in bases:
        _ensure_sim_collection(
            db,
            base,
            existing_names=existing_names,
            ttl_seconds=int(ttl_seconds),
            dry_run=dry_run,
            result=result,
        )
    _stamp_registry_kind(
        db,
        default_kind=default_registry_kind,
        dry_run=dry_run,
        result=result,
    )
    return result


# ── CLI ───────────────────────────────────────────────────────────────────


def _connect_mongo_db() -> Any:
    from pymongo import MongoClient  # local import keeps unit tests light

    uri = os.getenv("MONGO_URI") or (
        f"mongodb://{os.getenv('MONGO_HOST', 'localhost')}:{os.getenv('MONGO_PORT', '27017')}"
    )
    db_name = os.getenv("MONGO_DB", "trading_ai")
    return MongoClient(uri)[db_name]


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sim_namespace_init",
        description="Initialize Mongo schema for the sim/replay namespace (idempotent).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    p.add_argument(
        "--ttl-seconds",
        type=int,
        default=DEFAULT_TTL_SECONDS,
        help=f"TTL on *_sim collections (default: {DEFAULT_TTL_SECONDS} = 30d).",
    )
    p.add_argument(
        "--default-registry-kind",
        default="oos",
        help="Kind to stamp on existing un-kinded strategy_eval_runs rows (default: oos).",
    )
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db = _connect_mongo_db()
    result = apply_migration(
        db,
        ttl_seconds=int(args.ttl_seconds),
        default_registry_kind=str(args.default_registry_kind),
        dry_run=bool(args.dry_run),
    )
    import json

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
