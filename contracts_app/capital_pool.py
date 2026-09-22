"""Shared capital pool across buyer instruments (BankNifty/NIFTY/SENSEX/
FINNIFTY/MIDCPNIFTY) -- a single Mongo document, reserved/released with
atomic ``find_one_and_update`` calls, so multiple independent instrument
containers can safely share one pool of capital without double-spending it.

Born 2026-09-21: today each instrument container runs its own independent,
statically-configured ``RISK_CAPITAL_ALLOCATED`` with zero cross-instrument
coordination, even though all 5 containers already share one physical Dhan
broker account, one Redis, and one Mongo. This module is the first
genuinely cross-instrument-contended resource this codebase has needed --
a prior distributed lock (``Namespace.lock_key_for()``) was deliberately
removed in favor of per-instrument namespace isolation, so there is no
existing coordination primitive to reuse. Standard MongoDB
``find_one_and_update`` with a filter guard is the right tool: it is a
well-known, safe, single-document atomic read-modify-write, using the Mongo
instance every container already reaches.

Arbitration policy (explicit product decision, not an implementation
detail): first-come-first-served. Whoever's reservation lands first wins;
a later request that can't be satisfied is simply refused -- the caller
treats that exactly like an operator_halt block for this one entry, not an
error. A per-instrument cap keeps any single instrument from monopolizing
the whole pool even under FCFS; it is enforced in the SAME atomic filter as
the pool-wide check, not as a separate pre-check, so there is no window for
a second race between "check cap" and "check pool."

Safety-by-default: every call site that uses this module gates on
CAPITAL_POOL_ENABLED (default off) BEFORE ever importing/calling into this
module for the actual trading path -- see strategy_app/risk/manager.py.
This module itself has no opinion on that flag; it just implements the
primitive.

Collection name: "capital_pool" is deliberately NOT added to
contracts_app/sim_namespace.py's _NAMESPACED_BASES set. That omission is
the entire mechanism that makes Namespace.collection_for("capital_pool")
return the same literal name regardless of which instrument asks -- i.e.
genuinely shared, not per-instrument. See tests/test_capital_pool.py's
namespace-sharing tripwire test.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

try:
    from pymongo import MongoClient, ReturnDocument
except ImportError:  # pragma: no cover - pymongo is a hard dependency of every caller
    MongoClient = None  # type: ignore[assignment,misc]
    ReturnDocument = None  # type: ignore[assignment,misc]

from .sim_namespace import resolve_namespace
from .time_utils import isoformat_ist

logger = logging.getLogger(__name__)

COLLECTION_BASE = "capital_pool"
POOL_DOC_ID = "pool"

_client: Optional["MongoClient"] = None


def _collection_name() -> str:
    """Always "capital_pool" regardless of instrument -- see module docstring."""
    return resolve_namespace("live").collection_for(COLLECTION_BASE)


def _mongo_client() -> "MongoClient":
    """Lazily create and cache one MongoClient, matching the URI/host fallback
    chain already used independently at every other Mongo call site in this
    repo (e.g. persistence_app/health.py, market_data_dashboard/real_source.py).
    Deliberately duplicated here rather than imported from persistence_app --
    contracts_app must not depend on any of the *_app packages."""
    global _client
    if _client is not None:
        return _client
    if MongoClient is None:
        raise RuntimeError("pymongo is not installed")
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        _client = MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000)
    else:
        _client = MongoClient(
            host=str(os.getenv("MONGO_HOST") or "localhost"),
            port=int(os.getenv("MONGO_PORT") or "27017"),
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=5000,
        )
    return _client


def _db_name() -> str:
    return str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"


def _get_collection():
    return _mongo_client()[_db_name()][_collection_name()]


def reset_client_for_tests() -> None:
    """Test-only: drop the cached client so a test can point MONGO_HOST elsewhere."""
    global _client
    _client = None


def ensure_pool_document(total: float, *, instruments: list[str]) -> None:
    """Idempotent bootstrap -- safe to call on every process start.

    Uses $setOnInsert so an already-existing document (with whatever
    available/reserved state it has accumulated) is never reset. Changing
    the pool's total capital later is a deliberate, separate, manual
    operation -- not an automatic side effect of a config change.
    """
    coll = _get_collection()
    coll.update_one(
        {"_id": POOL_DOC_ID},
        {
            "$setOnInsert": {
                "total": float(total),
                "available": float(total),
                "reserved_by_instrument": {instr: 0.0 for instr in instruments},
                "updated_at": isoformat_ist(),
                "version": 0,
                "reconciliation": {"last_run_at": None, "last_corrected_at": None, "correction_count": 0},
            }
        },
        upsert=True,
    )


def try_reserve(instrument: str, amount: float, *, per_instrument_cap: float) -> tuple[bool, str]:
    """Atomically reserve *amount* for *instrument*, honoring both the
    pool-wide balance and the per-instrument cap in one round trip.

    Returns (True, "reserved") on success. On failure, returns (False, reason)
    where reason is "insufficient_pool_capital" or "instrument_cap_exceeded" --
    determined by a second, non-atomic, informational-only read purely for
    logging; the actual decision was already made atomically above.
    """
    if amount <= 0:
        return True, "zero_amount"
    coll = _get_collection()
    field = f"reserved_by_instrument.{instrument}"
    doc = coll.find_one_and_update(
        {
            "_id": POOL_DOC_ID,
            "available": {"$gte": amount},
            field: {"$lte": per_instrument_cap - amount},
        },
        {
            "$inc": {"available": -amount, field: amount, "version": 1},
            "$set": {"updated_at": isoformat_ist()},
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc is not None:
        return True, "reserved"
    current = coll.find_one({"_id": POOL_DOC_ID}) or {}
    if float(current.get("available", 0.0)) < amount:
        return False, "insufficient_pool_capital"
    return False, "instrument_cap_exceeded"


def release(instrument: str, amount: float) -> None:
    """Unconditionally return *amount* to the pool.

    Safe because callers only ever release an amount they previously,
    successfully reserved -- see RiskManager.release_capital_reservation(),
    which tracks its own single active reservation and is a no-op if there
    isn't one. This function itself does not (and cannot, from a single
    unconditional increment) verify that invariant -- it trusts the caller.
    """
    if amount <= 0:
        return
    coll = _get_collection()
    field = f"reserved_by_instrument.{instrument}"
    coll.find_one_and_update(
        {"_id": POOL_DOC_ID},
        {
            "$inc": {"available": amount, field: -amount, "version": 1},
            "$set": {"updated_at": isoformat_ist()},
        },
    )


def status() -> Optional[dict[str, Any]]:
    """Read-only snapshot of the pool document, for the dashboard and the
    reconciliation script. Returns None if the pool has never been
    initialized (ensure_pool_document was never called)."""
    coll = _get_collection()
    doc = coll.find_one({"_id": POOL_DOC_ID})
    if doc is None:
        return None
    doc.pop("_id", None)
    return doc


def force_set_reserved(reserved_by_instrument: dict[str, float], *, total: float) -> None:
    """Reconciliation-only: overwrite reserved/available to match a freshly
    computed ground truth (sum of actually-open positions). Not used on the
    normal reserve/release path -- see ops/capital_pool_reconcile.py."""
    coll = _get_collection()
    total_reserved = sum(reserved_by_instrument.values())
    coll.find_one_and_update(
        {"_id": POOL_DOC_ID},
        {
            "$set": {
                "reserved_by_instrument": dict(reserved_by_instrument),
                "available": float(total) - float(total_reserved),
                "updated_at": isoformat_ist(),
                "reconciliation.last_run_at": isoformat_ist(),
                "reconciliation.last_corrected_at": isoformat_ist(),
            },
            "$inc": {"reconciliation.correction_count": 1, "version": 1},
        },
    )
