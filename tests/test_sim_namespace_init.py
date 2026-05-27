"""Tests for ops.migrations.sim_namespace_init — SIM-2."""
from __future__ import annotations

import unittest
from typing import Any, Iterable, Mapping, Optional

from ops.migrations.sim_namespace_init import (
    DEFAULT_TTL_SECONDS,
    apply_migration,
)


# ── Minimal in-process Mongo fake ─────────────────────────────────────────


class _UpdateResult:
    def __init__(self, modified_count: int) -> None:
        self.modified_count = modified_count


class _FakeCollection:
    def __init__(self, docs: Optional[list[dict[str, Any]]] = None) -> None:
        self._docs: list[dict[str, Any]] = list(docs or [])
        # name -> {"keys": [...], "options": {...}}
        self.indexes: dict[str, dict[str, Any]] = {}

    # --- index API ---
    def index_information(self) -> dict[str, dict[str, Any]]:
        return {name: dict(meta) for name, meta in self.indexes.items()}

    def create_index(
        self,
        keys: list[tuple[str, int]],
        name: str,
        expireAfterSeconds: Optional[int] = None,
        **_kwargs: Any,
    ) -> str:
        if name in self.indexes:
            raise RuntimeError(f"index {name} already exists")
        meta: dict[str, Any] = {"keys": list(keys)}
        if expireAfterSeconds is not None:
            meta["expireAfterSeconds"] = int(expireAfterSeconds)
        self.indexes[name] = meta
        return name

    # --- doc API used by stamp_registry_kind ---
    def count_documents(self, filter: Mapping[str, Any]) -> int:  # noqa: A002
        return sum(1 for d in self._docs if self._matches(d, filter))

    def update_many(
        self,
        filter: Mapping[str, Any],  # noqa: A002
        update: Mapping[str, Any],
    ) -> _UpdateResult:
        modified = 0
        set_dict = update.get("$set", {}) or {}
        for doc in self._docs:
            if self._matches(doc, filter):
                doc.update(set_dict)
                modified += 1
        return _UpdateResult(modified)

    @staticmethod
    def _matches(doc: Mapping[str, Any], filter: Mapping[str, Any]) -> bool:  # noqa: A002
        for k, cond in filter.items():
            if isinstance(cond, dict) and "$exists" in cond:
                present = k in doc
                if bool(cond["$exists"]) != present:
                    return False
            elif doc.get(k) != cond:
                return False
        return True


class _FakeDatabase:
    """Mirrors pymongo: ``db[name]`` returns a handle without doing I/O;
    ``create_collection`` actually registers it; ``list_collection_names``
    returns only registered ones."""

    def __init__(self) -> None:
        self._handles: dict[str, _FakeCollection] = {}
        self._physical: set[str] = set()

    @property
    def collections(self) -> dict[str, _FakeCollection]:
        """Convenience for tests — only returns physically-created colls."""
        return {n: self._handles[n] for n in self._physical}

    def list_collection_names(self) -> list[str]:
        return list(self._physical)

    def create_collection(self, name: str) -> _FakeCollection:
        if name in self._physical:
            raise RuntimeError(f"collection {name} already exists")
        # Reuse the handle if a caller already grabbed one via __getitem__.
        coll = self._handles.setdefault(name, _FakeCollection())
        self._physical.add(name)
        return coll

    def __getitem__(self, name: str) -> _FakeCollection:
        # Non-destructive: returns a handle. The collection is not
        # "physically created" until create_collection is called.
        return self._handles.setdefault(name, _FakeCollection())


# ── Tests ─────────────────────────────────────────────────────────────────


_EXPECTED_SIM_COLLS = (
    "phase1_market_snapshots_sim",
    "strategy_votes_sim",
    "trade_signals_sim",
    "strategy_positions_sim",
    "strategy_decision_traces_sim",
    "market_depth_ticks_sim",
)


class TestApplyMigration(unittest.TestCase):
    def test_creates_all_sim_collections_first_run(self) -> None:
        db = _FakeDatabase()
        result = apply_migration(db)
        for coll in _EXPECTED_SIM_COLLS:
            self.assertIn(coll, db.collections, f"missing collection {coll}")
        self.assertEqual(
            set(result.sim_collections_created),
            set(_EXPECTED_SIM_COLLS),
        )

    def test_creates_ttl_index_with_30d_default(self) -> None:
        db = _FakeDatabase()
        apply_migration(db)
        for coll_name in _EXPECTED_SIM_COLLS:
            indexes = db.collections[coll_name].index_information()
            self.assertIn("ttl_created_at", indexes, coll_name)
            ttl = indexes["ttl_created_at"].get("expireAfterSeconds")
            self.assertEqual(ttl, DEFAULT_TTL_SECONDS)

    def test_creates_compound_run_id_created_at_index(self) -> None:
        db = _FakeDatabase()
        apply_migration(db)
        for coll_name in _EXPECTED_SIM_COLLS:
            indexes = db.collections[coll_name].index_information()
            self.assertIn("run_id_created_at", indexes, coll_name)

    def test_idempotent_second_run_creates_nothing(self) -> None:
        db = _FakeDatabase()
        apply_migration(db)
        result2 = apply_migration(db)
        self.assertEqual(result2.sim_collections_created, [])
        self.assertEqual(result2.ttl_indexes_created, [])
        self.assertEqual(result2.compound_indexes_created, [])
        self.assertEqual(
            set(result2.sim_collections_existing),
            set(_EXPECTED_SIM_COLLS),
        )

    def test_dry_run_writes_nothing(self) -> None:
        db = _FakeDatabase()
        result = apply_migration(db, dry_run=True)
        self.assertEqual(db.collections, {})
        self.assertTrue(result.dry_run)
        self.assertEqual(
            set(result.sim_collections_created),
            set(_EXPECTED_SIM_COLLS),
        )  # records intent
        self.assertTrue(any("WOULD create collection" in n for n in result.notes))

    def test_custom_ttl_seconds(self) -> None:
        db = _FakeDatabase()
        apply_migration(db, ttl_seconds=7 * 86400)
        ttl = db.collections["phase1_market_snapshots_sim"].index_information()[
            "ttl_created_at"
        ]["expireAfterSeconds"]
        self.assertEqual(ttl, 7 * 86400)


class TestRegistryKindStamping(unittest.TestCase):
    def test_stamps_un_kinded_rows_with_default_kind(self) -> None:
        db = _FakeDatabase()
        # Pre-seed registry with two rows lacking `kind`
        registry = db["strategy_eval_runs"]
        registry._docs.extend(
            [
                {"run_id": "r-1", "created_at": "2024-01-01"},
                {"run_id": "r-2", "created_at": "2024-01-02"},
            ]
        )
        result = apply_migration(db)
        self.assertEqual(result.registry_rows_stamped, 2)
        for doc in registry._docs:
            self.assertEqual(doc.get("kind"), "oos")

    def test_does_not_overwrite_existing_kind(self) -> None:
        db = _FakeDatabase()
        registry = db["strategy_eval_runs"]
        registry._docs.append(
            {"run_id": "r-live", "created_at": "2024-01-01", "kind": "live"}
        )
        registry._docs.append(
            {"run_id": "r-sim", "created_at": "2024-01-02", "kind": "sim"}
        )
        result = apply_migration(db)
        self.assertEqual(result.registry_rows_stamped, 0)
        kinds = sorted(d["kind"] for d in registry._docs)
        self.assertEqual(kinds, ["live", "sim"])

    def test_creates_kind_created_at_index(self) -> None:
        db = _FakeDatabase()
        apply_migration(db)
        idx = db["strategy_eval_runs"].index_information()
        self.assertIn("kind_created_at", idx)

    def test_custom_default_registry_kind(self) -> None:
        db = _FakeDatabase()
        registry = db["strategy_eval_runs"]
        registry._docs.append({"run_id": "r-1", "created_at": "2024-01-01"})
        apply_migration(db, default_registry_kind="live")
        self.assertEqual(registry._docs[0]["kind"], "live")

    def test_dry_run_does_not_stamp(self) -> None:
        db = _FakeDatabase()
        registry = db["strategy_eval_runs"]
        registry._docs.append({"run_id": "r-1", "created_at": "2024-01-01"})
        result = apply_migration(db, dry_run=True)
        self.assertEqual(result.registry_rows_stamped, 0)
        self.assertNotIn("kind", registry._docs[0])
        self.assertTrue(any("WOULD stamp" in n for n in result.notes))


class TestResultSerialization(unittest.TestCase):
    def test_to_dict_round_trips(self) -> None:
        db = _FakeDatabase()
        result = apply_migration(db)
        d = result.to_dict()
        self.assertIn("sim_collections_created", d)
        self.assertEqual(d["dry_run"], False)
        self.assertEqual(d["registry_index_created"], True)


if __name__ == "__main__":
    unittest.main()
