import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException

from market_data_dashboard.routes.schemas.sim import SimRunCreateRequest
from market_data_dashboard.routes.sim_routes import DashboardSimRouter


class _InsertOneResult:
    def __init__(self, inserted_id: str) -> None:
        self.inserted_id = inserted_id


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def sort(self, key, direction):  # noqa: ARG002
        reverse = direction == -1
        self._rows.sort(key=lambda row: str(row.get(key) or ""), reverse=reverse)
        return self

    def limit(self, n: int):
        return self._rows[: int(n)]


class _FakeCollection:
    def __init__(self) -> None:
        self._rows = []

    def insert_one(self, doc):
        self._rows.append(deepcopy(doc))
        return _InsertOneResult(str(doc.get("run_id") or ""))

    def update_one(self, query, update):
        rid = str(query.get("run_id") or "")
        for row in self._rows:
            if str(row.get("run_id") or "") != rid:
                continue
            for key, value in (update.get("$set") or {}).items():
                row[key] = value

    def find_one(self, query, projection=None):  # noqa: ARG002
        rid = str(query.get("run_id") or "")
        for row in self._rows:
            if str(row.get("run_id") or "") == rid:
                return deepcopy(row)
        return None

    def find(self, query, projection=None):  # noqa: ARG002
        out = []
        for row in self._rows:
            ok = True
            for key, value in query.items():
                if row.get(key) != value:
                    ok = False
                    break
            if ok:
                out.append(deepcopy(row))
        return _FakeCursor(out)

    def count_documents(self, query):
        count = 0
        for row in self._rows:
            ok = True
            for key, value in query.items():
                if row.get(key) != value:
                    ok = False
                    break
            if ok:
                count += 1
        return count


class _FakeDb:
    def __init__(self) -> None:
        self._collections = {"strategy_eval_runs": _FakeCollection()}

    def __getitem__(self, key):
        if key not in self._collections:
            self._collections[key] = _FakeCollection()
        return self._collections[key]


class SimRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.fake_db = _FakeDb()
        self.spawned = {"publisher": [], "consumer": [], "stopped": []}

        def _spawn_publisher(args, env):  # noqa: ARG001
            self.spawned["publisher"].append(list(args))
            return 12345

        def _spawn_consumer(run_id):
            self.spawned["consumer"].append(str(run_id))
            return f"container-{run_id}"

        def _stop_consumer(container_id):
            self.spawned["stopped"].append(str(container_id))

        self.router = DashboardSimRouter(
            get_db=lambda: self.fake_db,
            spawn_publisher=_spawn_publisher,
            spawn_consumer=_spawn_consumer,
            stop_consumer=_stop_consumer,
            get_image_digest=lambda: "sha256:test",
            run_dir_root=Path(self._tmp.name),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_run_rejects_unknown_env_key(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self.router.create_run(
                SimRunCreateRequest(
                    source_date="2024-08-01",
                    env_overrides={"RANDOM_VAR": "1"},
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_create_list_get_and_delete_run(self) -> None:
        created = self.router.create_run(
            SimRunCreateRequest(
                source_date="2024-08-01",
                source_coll="phase1_market_snapshots",
                label="smoke",
                speed=30.0,
                env_overrides={"STRATEGY_PROFILE_ID": "trader_master_v1"},
            )
        ).model_dump()
        run_id = str(created["run_id"])
        self.assertTrue(created["manifest_path"].endswith("manifest.json"))
        self.assertIn(run_id, created["stream_name"])
        self.assertEqual(len(self.spawned["publisher"]), 1)
        self.assertEqual(self.spawned["consumer"], [run_id])

        listing = self.router.list_runs(date=None, limit=20)
        rows = listing["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], run_id)
        self.assertEqual(rows[0]["kind"], "sim")

        detail = self.router.get_run(run_id)
        self.assertEqual(detail["run_id"], run_id)

        cancel = self.router.cancel_run(run_id)
        self.assertEqual(cancel["status"], "cancelled")
        self.assertEqual(len(self.spawned["stopped"]), 1)


if __name__ == "__main__":
    unittest.main()

