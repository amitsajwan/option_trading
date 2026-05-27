"""Tests for ops/sim/run_sim_publisher.py — SIM-3.

Uses lightweight in-process fakes (no fakeredis / mongomock dependency)
to match the existing repo testing style (unittest.mock-based, no new
runtime deps).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from unittest import mock

from contracts_app import SimManifest, resolve_namespace
from ops.sim.run_sim_publisher import (
    SENTINEL_TYPE,
    SimPublisher,
    _write_cancellation,
    _write_result,
    main,
)


# ── In-process fakes ──────────────────────────────────────────────────────


class _FakeRedis:
    """Records XADDs in insertion order; assigns synthetic IDs."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._counter = 0

    def xadd(
        self,
        name: str,
        fields: Mapping[str, Any],
        *,
        maxlen: Optional[int] = None,
        approximate: bool = False,
    ) -> str:
        self._counter += 1
        entry_id = f"100000-{self._counter}"
        self.streams.setdefault(name, []).append((entry_id, dict(fields)))
        # honour maxlen approximately like the real server
        if maxlen is not None and len(self.streams[name]) > maxlen:
            self.streams[name] = self.streams[name][-maxlen:]
        return entry_id


class _FakeCollection:
    """Minimal stand-in for a pymongo Collection."""

    def __init__(self, docs: list[Mapping[str, Any]]) -> None:
        self._docs = list(docs)

    def find(
        self,
        filter: Mapping[str, Any],  # noqa: A002
        projection: Optional[Mapping[str, Any]] = None,
    ) -> Iterable[Mapping[str, Any]]:
        date = filter.get("trade_date_ist")
        for doc in self._docs:
            if date is None or doc.get("trade_date_ist") == date:
                yield dict(doc)

    def count_documents(self, filter: Mapping[str, Any]) -> int:  # noqa: A002
        date = filter.get("trade_date_ist")
        return sum(1 for d in self._docs if d.get("trade_date_ist") == date)


def _make_snapshots(date: str, n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        sid = f"{date.replace('-','')}_{i:04d}"
        out.append(
            {
                "_id": f"oid-{sid}",
                "snapshot_id": sid,
                "trade_date_ist": date,
                "payload": {"snapshot": {"snapshot_id": sid, "fut_close": 55000 + i}},
                "meta": {"source": "snapshot_app"},
            }
        )
    return out


def _make_publisher(
    *,
    redis_client: Optional[_FakeRedis] = None,
    coll: Optional[_FakeCollection] = None,
    run_id: str = "test-run-001",
    source_date: str = "2026-05-27",
    label: str = "unit-test",
    speed: float = 60_000.0,  # one bar / ms in tests, effectively no sleep
    max_len: int = 1000,
    env_overrides: Optional[dict[str, str]] = None,
    image_digest: str = "sha256:test",
    run_dir_root: Optional[Path] = None,
) -> SimPublisher:
    redis_client = redis_client or _FakeRedis()
    coll = coll if coll is not None else _FakeCollection(_make_snapshots(source_date, 5))

    pub = SimPublisher(
        run_id=run_id,
        source_date=source_date,
        source_coll="phase1_market_snapshots",
        label=label,
        speed=speed,
        max_len=max_len,
        redis_client=redis_client,
        mongo_collection=coll,
        image_digest=image_digest,
        env_overrides=env_overrides or {},
        sleep_fn=lambda _t: None,  # never sleep in tests
        monotonic_fn=lambda: 0.0,  # constant clock → drift always 0
    )
    if run_dir_root is not None:
        # Redirect the per-run dir into the temp area for tests.
        object.__setattr__(pub, "_run_dir", Path(run_dir_root) / run_id)
    return pub


# ── Construction / validation ─────────────────────────────────────────────


class TestPublisherConstruction(unittest.TestCase):
    def test_minimal_construction(self) -> None:
        pub = _make_publisher()
        self.assertEqual(pub.run_id, "test-run-001")
        self.assertEqual(
            pub.stream_name,
            resolve_namespace("sim", run_id="test-run-001").stream_for("snapshots"),
        )

    def test_run_id_required(self) -> None:
        with self.assertRaises(ValueError):
            _make_publisher(run_id="")

    def test_source_date_required(self) -> None:
        with self.assertRaises(ValueError):
            _make_publisher(source_date="")

    def test_speed_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            _make_publisher(speed=0.0)
        with self.assertRaises(ValueError):
            _make_publisher(speed=-1.0)

    def test_max_len_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            _make_publisher(max_len=0)


# ── Manifest behaviour ────────────────────────────────────────────────────


class TestPublisherManifest(unittest.TestCase):
    def test_write_initial_manifest_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pub = _make_publisher(run_dir_root=Path(tmp))
            path = pub.write_initial_manifest()
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["run_id"], pub.run_id)
            self.assertEqual(data["kind"], "sim")
            self.assertEqual(data["source_date"], "2026-05-27")
            self.assertEqual(data["terminal_status"], "running")
            self.assertIn("config_hash", data)
            self.assertIn("git_commit", data)

    def test_manifest_write_is_idempotent_for_same_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pub = _make_publisher(run_dir_root=Path(tmp))
            first = pub.write_initial_manifest()
            second = pub.write_initial_manifest()
            self.assertEqual(first, second)

    def test_manifest_is_round_trippable_via_simmanifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pub = _make_publisher(run_dir_root=Path(tmp))
            path = pub.write_initial_manifest()
            revived = SimManifest.from_json(path.read_text(encoding="utf-8"))
            self.assertEqual(revived.run_id, pub.run_id)


# ── Run lifecycle ─────────────────────────────────────────────────────────


class TestPublisherRun(unittest.TestCase):
    def test_clean_run_publishes_all_then_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            redis_client = _FakeRedis()
            coll = _FakeCollection(_make_snapshots("2026-05-27", 5))
            pub = _make_publisher(
                redis_client=redis_client,
                coll=coll,
                run_dir_root=Path(tmp),
            )
            pub.write_initial_manifest()
            summary = pub.run()

            stream = redis_client.streams[pub.stream_name]
            # 5 snapshots + 1 sentinel
            self.assertEqual(len(stream), 6)
            # First 5 are snapshot type
            for entry_id, fields in stream[:5]:
                self.assertEqual(fields["type"], "snapshot")
                self.assertEqual(fields["source_mode"], "sim")
                self.assertEqual(fields["run_id"], pub.run_id)
            # Last entry is sentinel
            _, sentinel = stream[-1]
            self.assertEqual(sentinel["type"], SENTINEL_TYPE)
            self.assertEqual(sentinel["aborted"], "0")
            self.assertEqual(sentinel["total_published"], "5")

            self.assertEqual(summary["terminal_status"], "completed")
            self.assertEqual(summary["total_published"], 5)
            self.assertIsNotNone(summary["sentinel_id"])

    def test_run_drops_result_json_on_clean_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pub = _make_publisher(run_dir_root=Path(tmp))
            pub.write_initial_manifest()
            pub.run()
            result_path = pub.run_dir / "result.json"
            self.assertTrue(result_path.exists())
            data = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(data["terminal_status"], "completed")
            self.assertEqual(data["total_published"], 5)

    def test_abort_emits_sentinel_with_aborted_1_and_drops_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            redis_client = _FakeRedis()
            coll = _FakeCollection(_make_snapshots("2026-05-27", 5))
            pub = _make_publisher(
                redis_client=redis_client,
                coll=coll,
                run_dir_root=Path(tmp),
            )
            pub.write_initial_manifest()
            pub.request_stop(reason="test-abort")
            summary = pub.run()

            stream = redis_client.streams[pub.stream_name]
            # Loop exits before any snapshot is published — only sentinel.
            self.assertEqual(len(stream), 1)
            _, sentinel = stream[0]
            self.assertEqual(sentinel["type"], SENTINEL_TYPE)
            self.assertEqual(sentinel["aborted"], "1")
            self.assertEqual(sentinel["total_published"], "0")

            cancel_path = pub.run_dir / "cancellation.json"
            self.assertTrue(cancel_path.exists())
            self.assertFalse((pub.run_dir / "result.json").exists())
            self.assertEqual(summary["terminal_status"], "cancelled")

    def test_run_uses_per_run_stream_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r1 = _FakeRedis()
            r2 = _FakeRedis()
            p1 = _make_publisher(
                redis_client=r1,
                coll=_FakeCollection(_make_snapshots("2026-05-27", 2)),
                run_id="rrr-1",
                run_dir_root=Path(tmp),
            )
            p2 = _make_publisher(
                redis_client=r2,
                coll=_FakeCollection(_make_snapshots("2026-05-27", 2)),
                run_id="rrr-2",
                run_dir_root=Path(tmp),
            )
            p1.write_initial_manifest()
            p2.write_initial_manifest()
            p1.run()
            p2.run()
            self.assertIn(p1.stream_name, r1.streams)
            self.assertIn(p2.stream_name, r2.streams)
            self.assertNotEqual(p1.stream_name, p2.stream_name)

    def test_event_carries_discriminator_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            redis_client = _FakeRedis()
            pub = _make_publisher(
                redis_client=redis_client,
                coll=_FakeCollection(_make_snapshots("2026-05-27", 1)),
                label="r1s_no_window",
                run_dir_root=Path(tmp),
            )
            pub.write_initial_manifest()
            pub.run()
            entries = redis_client.streams[pub.stream_name]
            snap_entry = entries[0]
            _, fields = snap_entry
            self.assertEqual(fields["source_mode"], "sim")
            self.assertEqual(fields["sim_label"], "r1s_no_window")
            payload = json.loads(fields["payload"])
            self.assertEqual(payload["meta"]["source_mode"], "sim")
            self.assertEqual(payload["meta"]["run_id"], pub.run_id)
            self.assertEqual(payload["meta"]["sim_label"], "r1s_no_window")

    def test_count_source_reflects_date_filter(self) -> None:
        coll = _FakeCollection(
            _make_snapshots("2026-05-27", 3) + _make_snapshots("2026-05-26", 7)
        )
        pub = _make_publisher(coll=coll)
        self.assertEqual(pub.count_source(), 3)


# ── Marker-file helpers ───────────────────────────────────────────────────


class TestMarkerWriters(unittest.TestCase):
    def test_cancellation_write_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "run-x"
            _write_cancellation(d, "first", sentinel_id="100-1")
            first = (d / "cancellation.json").read_text(encoding="utf-8")
            _write_cancellation(d, "second", sentinel_id="100-2")
            second = (d / "cancellation.json").read_text(encoding="utf-8")
            self.assertEqual(first, second)  # first cancellation wins

    def test_result_write_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "run-y"
            _write_result(d, total_published=5, sentinel_id="100-1", terminal_status="completed")
            first = (d / "result.json").read_text(encoding="utf-8")
            _write_result(d, total_published=99, sentinel_id="100-2", terminal_status="completed")
            second = (d / "result.json").read_text(encoding="utf-8")
            self.assertEqual(first, second)


# ── CLI ──────────────────────────────────────────────────────────────────


class TestCli(unittest.TestCase):
    def test_invalid_env_overrides_json_rejected(self) -> None:
        rc = main(
            [
                "--run-id", "cli-test",
                "--source-date", "2026-05-27",
                "--env-overrides-json", "not-json",
            ]
        )
        self.assertEqual(rc, 2)

    def test_missing_required_args_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            main([])  # argparse exits with non-zero

    def test_no_matching_snapshots_returns_4(self) -> None:
        # Patch connection helpers to return our fakes.
        with mock.patch("ops.sim.run_sim_publisher._connect_redis", return_value=_FakeRedis()), \
             mock.patch(
                 "ops.sim.run_sim_publisher._connect_mongo_collection",
                 return_value=_FakeCollection([]),
             ), \
             tempfile.TemporaryDirectory() as tmp, \
             mock.patch(
                 "ops.sim.run_sim_publisher.resolve_namespace",
                 side_effect=lambda kind, run_id=None: _redirect_namespace(kind, run_id, Path(tmp)),
             ):
            rc = main(
                [
                    "--run-id", "cli-no-data",
                    "--source-date", "2099-01-01",
                ]
            )
            self.assertEqual(rc, 4)

    def test_clean_cli_run_returns_0(self) -> None:
        with mock.patch("ops.sim.run_sim_publisher._connect_redis", return_value=_FakeRedis()), \
             mock.patch(
                 "ops.sim.run_sim_publisher._connect_mongo_collection",
                 return_value=_FakeCollection(_make_snapshots("2026-05-27", 3)),
             ), \
             tempfile.TemporaryDirectory() as tmp, \
             mock.patch(
                 "ops.sim.run_sim_publisher.resolve_namespace",
                 side_effect=lambda kind, run_id=None: _redirect_namespace(kind, run_id, Path(tmp)),
             ):
            rc = main(
                [
                    "--run-id", "cli-clean",
                    "--source-date", "2026-05-27",
                    "--label", "cli-test",
                    "--speed", "10000",
                ]
            )
            self.assertEqual(rc, 0)


def _redirect_namespace(kind: str, run_id: Optional[str], root: Path):
    """Wrap resolve_namespace so run_dir_for() points under a tempdir for tests."""
    ns = resolve_namespace(kind, run_id)  # type: ignore[arg-type]

    class _Wrapped:
        # Forward everything except run_dir_for
        def __getattr__(self, item):
            return getattr(ns, item)

        def run_dir_for(self):
            return root / (run_id or kind)

        def stream_for(self, what):
            return ns.stream_for(what)

        def collection_for(self, base):
            return ns.collection_for(base)

        def state_key_for(self, key):
            return ns.state_key_for(key)

        def lock_key_for(self):
            return ns.lock_key_for()

        def transport(self):
            return ns.transport()

        @property
        def kind(self):
            return ns.kind

        @property
        def run_id(self):
            return ns.run_id

    return _Wrapped()


if __name__ == "__main__":
    unittest.main()
