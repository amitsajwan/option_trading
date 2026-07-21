"""A2+D2: streams is the only transport; ConsumerLock is gone."""

import os
import unittest
from unittest.mock import MagicMock, patch

from strategy_app.runtime.redis_snapshot_consumer import RedisSnapshotConsumer


class _FakeEngine:
    def on_session_start(self, trade_date): ...
    def on_session_end(self, trade_date): ...
    def evaluate(self, snapshot): return None


class StreamsDefaultTests(unittest.TestCase):
    def _make_consumer(self, **kwargs) -> RedisSnapshotConsumer:
        fake_client = MagicMock()
        return RedisSnapshotConsumer(engine=_FakeEngine(), client=fake_client, **kwargs)

    def test_streams_transport_is_default_when_env_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STRATEGY_CONSUMER_TRANSPORT", None)
            consumer = self._make_consumer()
        self.assertEqual(consumer._transport, "streams")

    def test_default_stream_name_is_live_stream(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STRATEGY_CONSUMER_TRANSPORT", None)
            os.environ.pop("STRATEGY_STREAM_NAME", None)
            consumer = self._make_consumer()
        self.assertEqual(consumer._stream_name, "stream:snapshots:live")

    def test_no_consumer_lock_attribute(self) -> None:
        """D2: ConsumerLock removed — _consumer_lock must not exist on consumer."""
        consumer = self._make_consumer()
        self.assertFalse(hasattr(consumer, "_consumer_lock"))

    def test_start_runs_streams_path(self) -> None:
        """start() always calls _start_streams — pubsub branch removed."""
        fake_bus = MagicMock()
        fake_bus.ensure_group.return_value = None
        fake_bus.consume.return_value = []
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STRATEGY_CONSUMER_TRANSPORT", None)
            consumer = self._make_consumer(bus=fake_bus)
        events = consumer.start(max_events=0)
        self.assertEqual(events, 0)
        fake_bus.ensure_group.assert_called_once()


if __name__ == "__main__":
    unittest.main()
