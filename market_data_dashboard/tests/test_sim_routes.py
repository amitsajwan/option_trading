import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException

from market_data_dashboard.routes.schemas.sim import SimRunCreateRequest
from market_data_dashboard.routes.sim_routes import (
    SIM_EVENT_CANCEL,
    SIM_EVENT_START,
    DashboardSimRouter,
)


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
        self.published: list[dict] = []

        def _publish(payload: dict) -> None:
            self.published.append(dict(payload))

        self.router = DashboardSimRouter(
            get_db=lambda: self.fake_db,
            publish_command=_publish,
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

    def test_create_list_get_and_cancel_run(self) -> None:
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
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["event_type"], SIM_EVENT_START)
        self.assertEqual(self.published[0]["run_id"], run_id)

        listing = self.router.list_runs(date=None, limit=20)
        rows = listing["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], run_id)
        self.assertEqual(rows[0]["kind"], "sim")

        detail = self.router.get_run(run_id)
        self.assertEqual(detail["run_id"], run_id)

        cancel = self.router.cancel_run(run_id)
        self.assertEqual(cancel["status"], "cancel_requested")
        self.assertEqual(self.published[1]["event_type"], SIM_EVENT_CANCEL)


if __name__ == "__main__":
    unittest.main()
