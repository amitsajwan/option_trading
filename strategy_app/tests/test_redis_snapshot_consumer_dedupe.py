import json
import os
import socket
import unittest
from datetime import date
from unittest.mock import patch

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

    def test_consumer_lock_stolen_when_owned_by_same_hostname(self) -> None:
        """Container restart case: a stale lock owned by a dead PID on the
        same hostname must be reclaimed atomically — not block the new
        process from starting. This was previously crashing every restart
        and recovering only via docker's restart-policy retry."""
        os.environ["STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC"] = "5"
        try:
            shared_client = _FakeRedis([])
            lock_key = "strategy_app:consumer_lock:market:snapshot:v1"
            # Lock from a "previous" process on this same hostname (different PID)
            stale_owner = f"{socket.gethostname()}:99999:dead0000:market:snapshot:v1"
            shared_client.set(lock_key, stale_owner, nx=True, ex=120)

            engine = _FakeEngine()
            consumer = RedisSnapshotConsumer(
                engine=engine,
                topic="market:snapshot:v1",
                client=shared_client,
                poll_interval_sec=0.001,
            )
            # _FakeRedis has no EVAL — code falls back to delete + retry SETNX.
            # Should reclaim on the second SETNX attempt without waiting.
            consumer._consumer_lock.acquire()
            current = shared_client.get(lock_key)
            self.assertEqual(current, consumer._consumer_lock.owner)
            self.assertNotEqual(current, stale_owner)
        finally:
            os.environ.pop("STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC", None)

    def test_consumer_lock_waits_and_acquires_when_other_owner_expires(self) -> None:
        """If a genuinely concurrent consumer's lock is about to expire, we
        wait for it then acquire — no crash."""
        os.environ["STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC"] = "5"
        try:
            shared_client = _FakeRedis([])
            lock_key = "strategy_app:consumer_lock:market:snapshot:v1"
            other_owner = "different-host-12345:7:abcdef:market:snapshot:v1"
            shared_client.set(lock_key, other_owner, nx=True, ex=120)

            engine = _FakeEngine()
            consumer = RedisSnapshotConsumer(
                engine=engine,
                topic="market:snapshot:v1",
                client=shared_client,
                poll_interval_sec=0.001,
            )

            # Simulate the lock expiring while we wait: on the 2nd Event.wait
            # call, free the lock so the next SETNX attempt succeeds.
            call_count = {"n": 0}

            def fake_wait(_self, _timeout):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    shared_client.delete(lock_key)
                return False  # not stopped

            with patch("threading.Event.wait", new=fake_wait):
                consumer._consumer_lock.acquire()
            self.assertEqual(shared_client.get(lock_key), consumer._consumer_lock.owner)
            self.assertGreaterEqual(call_count["n"], 1)
        finally:
            os.environ.pop("STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC", None)

    def test_consumer_lock_raises_after_max_wait_for_persistent_other_owner(self) -> None:
        """If a different-host lock persists past max_wait, we raise — operator
        needs to know there's a real duplicate consumer."""
        os.environ["STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC"] = "5"
        try:
            shared_client = _FakeRedis([])
            lock_key = "strategy_app:consumer_lock:market:snapshot:v1"
            other_owner = "different-host-12345:7:abcdef:market:snapshot:v1"
            shared_client.set(lock_key, other_owner, nx=True, ex=300)

            engine = _FakeEngine()
            consumer = RedisSnapshotConsumer(
                engine=engine,
                topic="market:snapshot:v1",
                client=shared_client,
                poll_interval_sec=0.001,
            )

            # Patch time.monotonic to fast-forward past deadline and Event.wait
            # to no-op — keeps the test under 100ms.
            t = {"now": 1000.0}
            real_monotonic = lambda: t["now"]  # noqa: E731

            def fake_wait(_self, _timeout):
                t["now"] += _timeout  # advance "time" by sleep duration
                return False

            with patch("strategy_app.runtime.redis_snapshot_consumer.time.monotonic",
                       side_effect=real_monotonic), \
                 patch("threading.Event.wait", new=fake_wait):
                with self.assertRaisesRegex(RuntimeError, "duplicate strategy consumer detected after waiting"):
                    consumer._consumer_lock.acquire()
            self.assertEqual(shared_client.get(lock_key), other_owner)
        finally:
            os.environ.pop("STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC", None)

    def test_consumer_lock_acquire_exits_cleanly_when_stop_requested(self) -> None:
        """If shutdown is requested while waiting on the lock, exit without
        raising — the app is closing, not trying to start."""
        os.environ["STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC"] = "5"
        try:
            shared_client = _FakeRedis([])
            lock_key = "strategy_app:consumer_lock:market:snapshot:v1"
            shared_client.set(lock_key, "different-host:1:x:market:snapshot:v1", nx=True, ex=300)

            engine = _FakeEngine()
            consumer = RedisSnapshotConsumer(
                engine=engine,
                topic="market:snapshot:v1",
                client=shared_client,
                poll_interval_sec=0.001,
            )

            def fake_wait_with_stop(_self, _timeout):
                return True  # simulate stop_event being set during wait

            with patch("threading.Event.wait", new=fake_wait_with_stop):
                consumer._consumer_lock.acquire()
            self.assertNotEqual(shared_client.get(lock_key), consumer._consumer_lock.owner)
        finally:
            os.environ.pop("STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC", None)

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
