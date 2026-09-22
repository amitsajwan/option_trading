"""Unit tests for contracts_app/capital_pool.py.

Uses a lightweight in-process fake Mongo collection (this repo's own
established convention -- see tests/test_run_sim_publisher.py -- rather
than mongomock, which isn't a dependency of this repo). The fake only
implements exactly the query/update shapes capital_pool.py actually issues,
not a general-purpose Mongo simulator.

The real concurrency proof (that try_reserve genuinely never oversubscribes
under actual concurrent access) is in test_capital_pool_concurrency.py,
which needs a reachable Mongo and skips gracefully otherwise -- a
single-threaded in-process fake cannot prove atomicity, only that the
sequential branching logic is correct.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

import pytest

from contracts_app import capital_pool
from contracts_app.sim_namespace import resolve_namespace


class _FakeCollection:
    """Narrow fake supporting exactly what capital_pool.py issues:
    find_one_and_update with an {"_id", "available": {"$gte"}, "<field>": {"$lte"}}
    filter and {"$inc", "$set"} update; find_one by _id; update_one with
    $setOnInsert + upsert."""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}

    def _matches(self, doc: dict[str, Any], filt: dict[str, Any]) -> bool:
        for key, cond in filt.items():
            if key == "_id":
                if doc.get("_id") != cond:
                    return False
                continue
            current = doc
            for part in key.split("."):
                if not isinstance(current, dict) or part not in current:
                    current = None
                    break
                current = current[part]
            if isinstance(cond, dict):
                if "$gte" in cond and not (current is not None and current >= cond["$gte"]):
                    return False
                if "$lte" in cond and not (current is not None and current <= cond["$lte"]):
                    return False
            else:
                if current != cond:
                    return False
        return True

    def _apply_inc(self, doc: dict[str, Any], path: str, delta: float) -> None:
        parts = path.split(".")
        target = doc
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = target.get(parts[-1], 0.0) + delta

    def _apply_set(self, doc: dict[str, Any], path: str, value: Any) -> None:
        parts = path.split(".")
        target = doc
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value

    def find_one_and_update(self, filt, update, return_document=None):
        doc_id = filt.get("_id")
        doc = self._docs.get(doc_id)
        if doc is None or not self._matches(doc, filt):
            return None
        for path, delta in (update.get("$inc") or {}).items():
            self._apply_inc(doc, path, delta)
        for path, value in (update.get("$set") or {}).items():
            self._apply_set(doc, path, value)
        return copy.deepcopy(doc)

    def find_one(self, filt):
        doc = self._docs.get(filt.get("_id"))
        return copy.deepcopy(doc) if doc is not None else None

    def update_one(self, filt, update, upsert=False):
        doc_id = filt.get("_id")
        doc = self._docs.get(doc_id)
        if doc is None:
            if not upsert:
                return
            doc = {"_id": doc_id}
            self._docs[doc_id] = doc
            for path, value in (update.get("$setOnInsert") or {}).items():
                self._apply_set(doc, path, value)


@pytest.fixture(autouse=True)
def _fake_collection(monkeypatch):
    coll = _FakeCollection()
    monkeypatch.setattr(capital_pool, "_get_collection", lambda: coll)
    return coll


INSTRUMENTS = ["BANKNIFTY", "NIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"]


def test_ensure_pool_document_creates_with_full_available():
    capital_pool.ensure_pool_document(400_000.0, instruments=INSTRUMENTS)
    doc = capital_pool.status()
    assert doc["total"] == 400_000.0
    assert doc["available"] == 400_000.0
    assert doc["reserved_by_instrument"] == {i: 0.0 for i in INSTRUMENTS}


def test_ensure_pool_document_is_idempotent():
    capital_pool.ensure_pool_document(400_000.0, instruments=INSTRUMENTS)
    ok, _ = capital_pool.try_reserve("NIFTY", 50_000.0, per_instrument_cap=1_000_000.0)
    assert ok
    # Calling ensure_pool_document again must NOT reset the already-modified doc.
    capital_pool.ensure_pool_document(400_000.0, instruments=INSTRUMENTS)
    doc = capital_pool.status()
    assert doc["available"] == 350_000.0
    assert doc["reserved_by_instrument"]["NIFTY"] == 50_000.0


def test_try_reserve_succeeds_and_decrements_available():
    capital_pool.ensure_pool_document(400_000.0, instruments=INSTRUMENTS)
    ok, reason = capital_pool.try_reserve("BANKNIFTY", 100_000.0, per_instrument_cap=200_000.0)
    assert ok and reason == "reserved"
    doc = capital_pool.status()
    assert doc["available"] == 300_000.0
    assert doc["reserved_by_instrument"]["BANKNIFTY"] == 100_000.0


def test_try_reserve_fails_on_insufficient_pool_capital():
    capital_pool.ensure_pool_document(100_000.0, instruments=INSTRUMENTS)
    ok, reason = capital_pool.try_reserve("NIFTY", 150_000.0, per_instrument_cap=1_000_000.0)
    assert not ok
    assert reason == "insufficient_pool_capital"
    # A failed reservation must not have changed anything.
    doc = capital_pool.status()
    assert doc["available"] == 100_000.0


def test_try_reserve_fails_on_instrument_cap_even_with_pool_capacity_free():
    capital_pool.ensure_pool_document(400_000.0, instruments=INSTRUMENTS)
    cap = 100_000.0
    ok1, _ = capital_pool.try_reserve("SENSEX", 90_000.0, per_instrument_cap=cap)
    assert ok1
    # Pool has 310,000 free, but SENSEX itself would exceed its own 100,000 cap.
    ok2, reason2 = capital_pool.try_reserve("SENSEX", 20_000.0, per_instrument_cap=cap)
    assert not ok2
    assert reason2 == "instrument_cap_exceeded"
    doc = capital_pool.status()
    assert doc["reserved_by_instrument"]["SENSEX"] == 90_000.0  # unchanged by the failed 2nd attempt


def test_release_restores_available_and_instrument_reserved():
    capital_pool.ensure_pool_document(400_000.0, instruments=INSTRUMENTS)
    capital_pool.try_reserve("FINNIFTY", 80_000.0, per_instrument_cap=1_000_000.0)
    capital_pool.release("FINNIFTY", 80_000.0)
    doc = capital_pool.status()
    assert doc["available"] == 400_000.0
    assert doc["reserved_by_instrument"]["FINNIFTY"] == 0.0


def test_release_zero_or_negative_is_a_no_op():
    capital_pool.ensure_pool_document(400_000.0, instruments=INSTRUMENTS)
    capital_pool.release("NIFTY", 0.0)
    capital_pool.release("NIFTY", -5.0)
    doc = capital_pool.status()
    assert doc["available"] == 400_000.0


def test_status_returns_none_before_initialization():
    assert capital_pool.status() is None


# ---------------------------------------------------------------------------
# Namespace-sharing tripwire (not the fake collection -- pure sim_namespace logic)
# ---------------------------------------------------------------------------

def test_capital_pool_collection_name_is_shared_across_instruments():
    """capital_pool must NOT be added to sim_namespace's _NAMESPACED_BASES --
    that omission is the entire mechanism making the pool genuinely shared.
    This test is the tripwire: if a future edit adds "capital_pool" to
    _NAMESPACED_BASES, every instrument would silently get its own pool
    document instead of sharing one, defeating the whole feature."""
    names = {
        resolve_namespace("live", instrument=instr).collection_for(capital_pool.COLLECTION_BASE)
        for instr in INSTRUMENTS
    }
    assert names == {"capital_pool"}, (
        f"expected all instruments to resolve to the single shared 'capital_pool' "
        f"collection name, got: {names}"
    )
