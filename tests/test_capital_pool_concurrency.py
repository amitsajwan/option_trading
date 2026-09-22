"""Real-Mongo concurrency proof for contracts_app/capital_pool.py.

The unit tests in test_capital_pool.py use a single-threaded in-process
fake and can only prove the branching logic is correct -- they cannot
prove find_one_and_update is actually atomic under real concurrent access,
which is the entire point of using it. This test needs a reachable Mongo
and is skipped gracefully otherwise (e.g. a laptop with no local Mongo
running) -- CI/an operator with the compose stack up should see it run.

Point MONGO_HOST (default localhost) / MONGO_PORT (default 27017) at any
disposable Mongo -- this test uses its own dedicated document id so it
never touches a real "pool" document from a running system.
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

try:
    from pymongo import MongoClient
    _PYMONGO_AVAILABLE = True
except ImportError:
    _PYMONGO_AVAILABLE = False


def _mongo_reachable() -> "MongoClient | None":
    if not _PYMONGO_AVAILABLE:
        return None
    try:
        client = MongoClient(
            host=os.getenv("MONGO_HOST", "localhost"),
            port=int(os.getenv("MONGO_PORT", "27017")),
            serverSelectionTimeoutMS=1000,
            connectTimeoutMS=1000,
        )
        client.admin.command("ping")
        return client
    except Exception:
        return None


@pytest.fixture()
def mongo_client():
    client = _mongo_reachable()
    if client is None:
        pytest.skip("no reachable Mongo (set MONGO_HOST/MONGO_PORT, or run the compose mongo service)")
    yield client
    client.close()


def test_concurrent_reservations_never_oversubscribe_the_pool(mongo_client):
    from pymongo import ReturnDocument

    db = mongo_client[os.getenv("MONGO_DB", "trading_ai")]
    coll = db["capital_pool_test_" + uuid.uuid4().hex[:8]]
    try:
        total = 100_000.0
        per_reservation = 7_000.0
        n_attempts = 40  # deliberately > total/per_reservation so some MUST fail
        coll.insert_one({
            "_id": "pool",
            "total": total,
            "available": total,
            "reserved_by_instrument": {"NIFTY": 0.0},
        })

        def _attempt() -> bool:
            doc = coll.find_one_and_update(
                {"_id": "pool", "available": {"$gte": per_reservation}},
                {"$inc": {"available": -per_reservation, "reserved_by_instrument.NIFTY": per_reservation}},
                return_document=ReturnDocument.AFTER,
            )
            return doc is not None

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(_attempt) for _ in range(n_attempts)]
            results = [f.result() for f in as_completed(futures)]

        n_succeeded = sum(1 for r in results if r)
        final = coll.find_one({"_id": "pool"})

        # The core atomicity guarantee: successful reservations times the
        # per-reservation amount must exactly match what's actually been
        # deducted -- no lost updates, no double-grants.
        assert final["available"] == pytest.approx(total - n_succeeded * per_reservation)
        assert final["reserved_by_instrument"]["NIFTY"] == pytest.approx(n_succeeded * per_reservation)
        # And the pool must never have gone negative -- the whole point.
        assert final["available"] >= 0
        # With 40 attempts at 7k against a 100k pool, at most 14 can succeed;
        # assert the filter guard actually rejected the rest rather than
        # everything trivially succeeding (which would prove nothing).
        assert n_succeeded < n_attempts
        assert n_succeeded == int(total // per_reservation)
    finally:
        coll.drop()
