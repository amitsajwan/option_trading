import json
import unittest
from datetime import date

from contracts_app import build_snapshot_event
from strategy_app.runtime.redis_snapshot_consumer import RedisSnapshotConsumer


def _snapshot_event(snapshot_id: str, ts: str, *, run_id: str = "sim-run-1") -> dict:
    return build_snapshot_event(
        snapshot={
            "snapshot_id": snapshot_id,
            "session_context": {
                "snapshot_id": snapshot_id,
                "timestamp": ts,
                "date": ts[:10],
            },
        },
        source="test",
        published_at=ts,
        metadata={"run_id": run_id, "source_mode": "sim"},
    )


def _stream_snapshot_fields(event: dict) -> dict[str, str]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return {
        "type": "snapshot",
        "run_id": str(metadata.get("run_id") or ""),
        "source_mode": str(metadata.get("source_mode") or "sim"),
        "sim_label": "unit",
        "payload": json.dumps(event),
    }


def _sentinel_fields(run_id: str = "sim-run-1", *, aborted: str = "0", total: int = 0) -> dict[str, str]:
    return {
        "type": "sentinel",
        "run_id": run_id,
        "aborted": aborted,
        "total_published": str(total),
    }


class _FakeStreamRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: set[tuple[str, str]] = set()
        self.pending: dict[tuple[str, str, str], list[tuple[str, dict[str, str]]]] = {}
        self.acked: list[tuple[str, str, str]] = []
        self._counter = 0

    def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self._counter += 1
        entry_id = f"1000-{self._counter}"
        self.streams.setdefault(stream, []).append((entry_id, dict(fields)))
        return entry_id

    def xgroup_create(self, stream: str, group: str, id: str = "0", mkstream: bool = True):  # noqa: A002, ARG002
        key = (stream, group)
        if key in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups.add(key)
        self.streams.setdefault(stream, [])
        return True

    def xreadgroup(self, group: str, consumer: str, streams: dict[str, str], count: int = 50, block: int = 5000):  # noqa: ARG002
        stream, read_id = next(iter(streams.items()))
        pending_key = (stream, group, consumer)
        if read_id == "0":
            entries = self.pending.get(pending_key, [])[:count]
            return [(stream, entries)] if entries else []

        delivered_ids = {
            entry_id
            for (pending_stream, pending_group, _consumer), pending_entries in self.pending.items()
            if pending_stream == stream and pending_group == group
            for entry_id, _fields in pending_entries
        }
        entries = [
            (entry_id, fields)
            for entry_id, fields in self.streams.get(stream, [])
            if entry_id not in delivered_ids
        ][:count]
        if entries:
            self.pending.setdefault(pending_key, []).extend(entries)
            return [(stream, entries)]
        return []

    def xack(self, stream: str, group: str, entry_id: str):
        self.acked.append((stream, group, entry_id))
        for key, entries in list(self.pending.items()):
            pending_stream, pending_group, _consumer = key
            if pending_stream == stream and pending_group == group:
                self.pending[key] = [(eid, fields) for eid, fields in entries if eid != entry_id]
        return 1


class _FakeEngine:
    def __init__(self) -> None:
        self.starts: list[date] = []
        self.ends: list[date] = []
        self.evaluated_snapshot_ids: list[str] = []
        self.contexts: list[tuple[str | None, dict]] = []

    def set_run_context(self, run_id, metadata) -> None:
        self.contexts.append((run_id, dict(metadata or {})))

    def on_session_start(self, trade_date: date) -> None:
        self.starts.append(trade_date)

    def on_session_end(self, trade_date: date) -> None:
        self.ends.append(trade_date)

    def evaluate(self, snapshot):
        self.evaluated_snapshot_ids.append(str(snapshot.get("snapshot_id") or ""))
        return None


class _CrashOnSecondEngine(_FakeEngine):
    def evaluate(self, snapshot):
        super().evaluate(snapshot)
        if len(self.evaluated_snapshot_ids) == 2:
            raise RuntimeError("boom")
        return None


class RedisStreamsConsumerTests(unittest.TestCase):
    def test_stream_of_events_then_sentinel_processes_and_exits(self) -> None:
        redis_client = _FakeStreamRedis()
        stream = "stream:snapshots:sim:sim-run-1"
        for i in range(5):
            redis_client.xadd(
                stream,
                _stream_snapshot_fields(
                    _snapshot_event(f"20260527_{i:04d}", "2026-05-27T09:15:00+05:30")
                ),
            )
        sentinel_id = redis_client.xadd(stream, _sentinel_fields(total=5))

        engine = _FakeEngine()
        consumer = RedisSnapshotConsumer(
            engine=engine,
            client=redis_client,  # type: ignore[arg-type]
            transport="streams",
            stream_name=stream,
            stream_consumer_name="consumer-test",
            poll_interval_sec=0.001,
        )

        consumed = consumer.start()

        self.assertEqual(consumed, 5)
        self.assertEqual(engine.evaluated_snapshot_ids, [f"20260527_{i:04d}" for i in range(5)])
        self.assertEqual(len(engine.starts), 1)
        self.assertEqual(len(engine.ends), 1)
        acked_ids = [entry_id for _stream, _group, entry_id in redis_client.acked]
        self.assertNotIn(sentinel_id, acked_ids)

    def test_stream_metadata_reaches_run_context(self) -> None:
        redis_client = _FakeStreamRedis()
        stream = "stream:snapshots:sim:sim-run-ctx"
        event = _snapshot_event("20260527_0001", "2026-05-27T09:15:00+05:30", run_id="sim-run-ctx")
        redis_client.xadd(stream, _stream_snapshot_fields(event))
        redis_client.xadd(stream, _sentinel_fields(run_id="sim-run-ctx", total=1))

        engine = _FakeEngine()
        consumer = RedisSnapshotConsumer(
            engine=engine,
            client=redis_client,  # type: ignore[arg-type]
            transport="streams",
            stream_name=stream,
            stream_consumer_name="consumer-test",
            poll_interval_sec=0.001,
        )

        consumer.start()

        self.assertEqual(engine.contexts[0][0], "sim-run-ctx")
        self.assertEqual(engine.contexts[0][1]["source_mode"], "sim")
        self.assertEqual(engine.contexts[0][1]["sim_label"], "unit")

    def test_pending_entry_is_reprocessed_after_crash(self) -> None:
        redis_client = _FakeStreamRedis()
        stream = "stream:snapshots:sim:sim-run-restart"
        for i in range(3):
            redis_client.xadd(
                stream,
                _stream_snapshot_fields(
                    _snapshot_event(f"20260527_{i:04d}", "2026-05-27T09:15:00+05:30")
                ),
            )
        redis_client.xadd(stream, _sentinel_fields(total=3))

        first_engine = _CrashOnSecondEngine()
        first = RedisSnapshotConsumer(
            engine=first_engine,
            client=redis_client,  # type: ignore[arg-type]
            transport="streams",
            stream_name=stream,
            stream_consumer_name="consumer-test",
            poll_interval_sec=0.001,
        )
        with self.assertRaisesRegex(RuntimeError, "boom"):
            first.start()

        self.assertEqual(first_engine.evaluated_snapshot_ids, ["20260527_0000", "20260527_0001"])

        second_engine = _FakeEngine()
        second = RedisSnapshotConsumer(
            engine=second_engine,
            client=redis_client,  # type: ignore[arg-type]
            transport="streams",
            stream_name=stream,
            stream_consumer_name="consumer-test",
            poll_interval_sec=0.001,
        )

        consumed = second.start()

        self.assertEqual(consumed, 2)
        self.assertEqual(second_engine.evaluated_snapshot_ids, ["20260527_0001", "20260527_0002"])

    def test_aborted_sentinel_exits_without_processing(self) -> None:
        redis_client = _FakeStreamRedis()
        stream = "stream:snapshots:sim:aborted"
        sentinel_id = redis_client.xadd(stream, _sentinel_fields(run_id="aborted", aborted="1", total=0))

        engine = _FakeEngine()
        consumer = RedisSnapshotConsumer(
            engine=engine,
            client=redis_client,  # type: ignore[arg-type]
            transport="streams",
            stream_name=stream,
            stream_consumer_name="consumer-test",
            poll_interval_sec=0.001,
        )

        consumed = consumer.start()

        self.assertEqual(consumed, 0)
        self.assertEqual(engine.evaluated_snapshot_ids, [])
        self.assertEqual(redis_client.acked, [])
        self.assertEqual(sentinel_id, "1000-1")


if __name__ == "__main__":
    unittest.main()
