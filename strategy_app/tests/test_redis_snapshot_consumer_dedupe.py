import json
import os
import unittest
from datetime import date

from strategy_app.runtime.redis_snapshot_consumer import RedisSnapshotConsumer


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


class _FakePubSub:
    def __init__(self, payloads: list[dict]) -> None:
        self._messages = [{"data": json.dumps(payload)} for payload in payloads]
        self.subscribed: list[str] = []
        self.closed = False

    def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    def get_message(self, ignore_subscribe_messages=True, timeout=1.0):  # noqa: ARG002
        if self._messages:
            return self._messages.pop(0)
        return None

    def close(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self, payloads: list[dict]) -> None:
        self._pubsub = _FakePubSub(payloads)
        self._store: dict[str, str] = {}

    def pubsub(self, ignore_subscribe_messages=True):  # noqa: ARG002
        return self._pubsub

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):  # noqa: ARG002
        if nx and key in self._store:
            return False
        self._store[key] = value
        return True

    def get(self, key: str):
        return self._store.get(key)

    def expire(self, key: str, ttl: int):  # noqa: ARG002
        return key in self._store

    def delete(self, key: str):
        existed = key in self._store
        if existed:
            del self._store[key]
        return 1 if existed else 0


class RedisSnapshotConsumerDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_lock_enabled = os.environ.get("STRATEGY_SINGLE_CONSUMER_LOCK_ENABLED")
        os.environ["STRATEGY_SINGLE_CONSUMER_LOCK_ENABLED"] = "1"

    def tearDown(self) -> None:
        if self._env_lock_enabled is None:
            os.environ.pop("STRATEGY_SINGLE_CONSUMER_LOCK_ENABLED", None)
        else:
            os.environ["STRATEGY_SINGLE_CONSUMER_LOCK_ENABLED"] = self._env_lock_enabled

    def test_skips_duplicate_snapshot_ids(self) -> None:
        payloads = [
            _event("20260306_1002", "2026-03-06T10:02:00+05:30"),
            _event("20260306_1002", "2026-03-06T10:02:00+05:30"),
            _event("20260306_1003", "2026-03-06T10:03:00+05:30"),
        ]
        engine = _FakeEngine()
        consumer = RedisSnapshotConsumer(
            engine=engine,
            topic="market:snapshot:v1",
            client=_FakeRedis(payloads),
            poll_interval_sec=0.001,
        )

        consumed = consumer.start(max_events=2)

        self.assertEqual(consumed, 2)
        self.assertEqual(engine.evaluated_snapshot_ids, ["20260306_1002", "20260306_1003"])
        self.assertEqual(len(engine.starts), 1)
        self.assertEqual(len(engine.ends), 1)

    def test_raises_on_duplicate_consumer_lock(self) -> None:
        payloads: list[dict] = []
        shared_client = _FakeRedis(payloads)
        existing_key = "strategy_app:consumer_lock:market:snapshot:v1"
        shared_client.set(existing_key, "other-owner", nx=True, ex=120)
        engine = _FakeEngine()
        consumer = RedisSnapshotConsumer(
            engine=engine,
            topic="market:snapshot:v1",
            client=shared_client,
            poll_interval_sec=0.001,
        )

        with self.assertRaisesRegex(RuntimeError, "duplicate strategy consumer detected"):
            consumer.start(max_events=0)

    def test_session_start_still_runs_when_prior_session_end_fails(self) -> None:
        payloads = [
            _event("20260306_1514", "2026-03-06T15:14:00+05:30"),
            _event("20260307_0915", "2026-03-07T09:15:00+05:30"),
        ]
        engine = _FailingEndEngine()
        consumer = RedisSnapshotConsumer(
            engine=engine,
            topic="market:snapshot:v1",
            client=_FakeRedis(payloads),
            poll_interval_sec=0.001,
        )

        consumed = consumer.start(max_events=2)

        self.assertEqual(consumed, 2)
        self.assertEqual(engine.starts, [date(2026, 3, 6), date(2026, 3, 7)])
        self.assertEqual(engine.evaluated_snapshot_ids, ["20260306_1514", "20260307_0915"])


if __name__ == "__main__":
    unittest.main()
