"""D2: ConsumerLock removed. Tests migrated from pubsub to streams transport."""
import json
import unittest
from datetime import date

from strategy_app.runtime.redis_snapshot_consumer import RedisSnapshotConsumer

# Re-exported for test_consumer_lock.py and test_redis_snapshot_consumer_streams_default.py
# which import _FakeRedis from this module.
_FakeRedis = None  # see _FakeStreamRedis below — keep name for import compatibility


def _event(snapshot_id: str, ts: str) -> dict:
    return {
        "event_type": "market_snapshot",
        "event_version": "1.0",
        "event_id": f"evt-{snapshot_id}",
        "source": "test",
        "published_at": ts,
        "snapshot_id": snapshot_id,
        "snapshot": {
            "snapshot_id": snapshot_id,
            "session_context": {
                "snapshot_id": snapshot_id,
                "timestamp": ts,
                "date": ts[:10],
            },
        },
        "metadata": {},
    }


def _stream_entry(snapshot_id: str, ts: str, entry_id: str) -> tuple:
    payload = json.dumps(_event(snapshot_id, ts), ensure_ascii=False)
    return (entry_id, {"payload": payload})


class _FakeEngine:
    def __init__(self) -> None:
        self.starts: list[date] = []
        self.ends: list[date] = []
        self.evaluated_snapshot_ids: list[str] = []

    def set_run_context(self, _run_id, _metadata) -> None:
        return None

    def on_session_start(self, trade_date: date) -> None:
        self.starts.append(trade_date)

    def on_session_end(self, trade_date: date) -> None:
        self.ends.append(trade_date)

    def evaluate(self, snapshot):
        self.evaluated_snapshot_ids.append(str(snapshot.get("snapshot_id") or ""))
        return None


class _FailingEndEngine(_FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self._failed_once = False

    def on_session_end(self, trade_date: date) -> None:
        self.ends.append(trade_date)
        if not self._failed_once:
            self._failed_once = True
            raise RuntimeError("boom")


class _FakeStreamRedis:
    """Minimal Redis fake supporting xreadgroup + xack + xgroup_create for tests."""

    def __init__(self, entries: list[tuple]) -> None:
        self._pending = list(entries)
        self._acked: list[str] = []
        self._exhausted = False

    def xgroup_create(self, *args, **kwargs):
        pass

    def xreadgroup(self, group, consumer, streams, count=50, block=5000):
        stream_name = list(streams.keys())[0]
        stream_id = list(streams.values())[0]
        if stream_id == "0":
            return []
        batch = self._pending[:count]
        self._pending = self._pending[count:]
        if not batch and not self._exhausted:
            self._exhausted = True
            return []
        return [(stream_name, batch)] if batch else []

    def xack(self, stream, group, *entry_ids):
        self._acked.extend(entry_ids)


# Keep _FakeRedis name importable (streams_default test imports it)
_FakeRedis = _FakeStreamRedis


def _make_consumer(engine, entries, stream_name="stream:snapshots:live"):
    client = _FakeStreamRedis(entries)
    return RedisSnapshotConsumer(
        engine=engine,
        stream_name=stream_name,
        client=client,
        transport="streams",
        poll_interval_sec=0.001,
    ), client


class RedisSnapshotConsumerDedupeTests(unittest.TestCase):
    def test_skips_duplicate_snapshot_ids(self) -> None:
        entries = [
            _stream_entry("20260306_1002", "2026-03-06T10:02:00+05:30", "1-1"),
            _stream_entry("20260306_1002", "2026-03-06T10:02:00+05:30", "1-2"),
            _stream_entry("20260306_1003", "2026-03-06T10:03:00+05:30", "1-3"),
        ]
        engine = _FakeEngine()
        consumer, _ = _make_consumer(engine, entries)

        consumed = consumer.start(max_events=2)

        self.assertEqual(consumed, 2)
        self.assertEqual(engine.evaluated_snapshot_ids, ["20260306_1002", "20260306_1003"])
        self.assertEqual(len(engine.starts), 1)
        self.assertEqual(len(engine.ends), 1)

    def test_session_start_still_runs_when_prior_session_end_fails(self) -> None:
        entries = [
            _stream_entry("20260306_1514", "2026-03-06T15:14:00+05:30", "2-1"),
            _stream_entry("20260307_0915", "2026-03-07T09:15:00+05:30", "2-2"),
        ]
        engine = _FailingEndEngine()
        consumer, _ = _make_consumer(engine, entries)

        consumed = consumer.start(max_events=2)

        self.assertEqual(consumed, 2)
        self.assertEqual(engine.starts, [date(2026, 3, 6), date(2026, 3, 7)])
        self.assertEqual(engine.evaluated_snapshot_ids, ["20260306_1514", "20260307_0915"])

    def test_acks_each_processed_entry(self) -> None:
        entries = [
            _stream_entry("20260306_1002", "2026-03-06T10:02:00+05:30", "3-1"),
            _stream_entry("20260306_1003", "2026-03-06T10:03:00+05:30", "3-2"),
        ]
        engine = _FakeEngine()
        consumer, client = _make_consumer(engine, entries)
        consumer.start(max_events=2)
        self.assertEqual(sorted(client._acked), ["3-1", "3-2"])


if __name__ == "__main__":
    unittest.main()
