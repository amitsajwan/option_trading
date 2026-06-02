"""A2: verify streams is the default transport and ConsumerLock is not acquired."""

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
        fake_client.pubsub.return_value = MagicMock()
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

    def test_streams_transport_no_lock_acquired(self) -> None:
        """When transport=streams, start() must never call ConsumerLock.acquire()."""
        fake_bus = MagicMock()
        fake_bus.ensure_group.return_value = None
        # Return empty batch immediately so _start_streams exits after draining PEL
        fake_bus.consume.return_value = []

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STRATEGY_CONSUMER_TRANSPORT", None)
            consumer = self._make_consumer(bus=fake_bus)

        with patch.object(consumer._consumer_lock, "acquire") as mock_acquire:
            consumer.start(max_events=0)
            mock_acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
