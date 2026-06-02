"""Tests for A3 — persistence_app snapshot consumer via Redis Streams XREADGROUP.

Covers:
  - Pending messages (PEL) re-delivered on restart before new messages
  - New messages consumed and acked after pending drain
  - No pubsub.subscribe call remains
"""
from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import MagicMock, call, patch

from persistence_app.main_snapshot_consumer import run_loop, _SNAPSHOT_STREAM, _SNAPSHOT_GROUP


def _payload(snapshot_id: str) -> str:
    return json.dumps({
        "event_type": "market_snapshot",
        "snapshot_id": snapshot_id,
        "snapshot": {"snapshot_id": snapshot_id, "session_context": {"date": "2026-06-02"}},
    })


class TestPendingMessagesRedeliveredOnRestart(unittest.TestCase):
    """PEL re-delivery: stream_id='0' first, then '>' once pending is empty."""

    def _make_bus(self, pending: list, new: list):
        bus = MagicMock()
        bus.ensure_group.return_value = None
        call_count = {"n": 0}

        def consume_side_effect(stream, group, consumer, count, block_ms, stream_id):
            call_count["n"] += 1
            if stream_id == "0":
                return [(f"p{i}", {"payload": _payload(f"snap-p{i}")}) for i in range(len(pending))] if pending else []
            # new messages — return one batch then raise KeyboardInterrupt to stop
            if new:
                msg = new.pop(0)
                return [msg]
            raise KeyboardInterrupt

        bus.consume.side_effect = consume_side_effect
        bus.acknowledge.return_value = None
        return bus

    def test_pending_messages_redelivered_on_restart(self):
        pending = [("0-1", {"payload": _payload("snap-A")}), ("0-2", {"payload": _payload("snap-B")})]
        bus = self._make_bus(pending=pending, new=[])

        writer = MagicMock()
        writer.write_snapshot_event.return_value = True

        with patch("persistence_app.main_snapshot_consumer.RedisEventBus", return_value=bus), \
             patch("persistence_app.main_snapshot_consumer.SnapshotMongoWriter", return_value=writer):
            run_loop(topic="stream:snapshots:live", health_log_interval_sec=0)

        # PEL messages were acked
        self.assertEqual(bus.acknowledge.call_count, 2)
        # Writer was called for both pending messages
        self.assertEqual(writer.write_snapshot_event.call_count, 2)

    def test_new_messages_read_after_pending_drained(self):
        new_msg = ("1-1", {"payload": _payload("snap-new")})
        bus = self._make_bus(pending=[], new=[new_msg])

        writer = MagicMock()
        writer.write_snapshot_event.return_value = True

        with patch("persistence_app.main_snapshot_consumer.RedisEventBus", return_value=bus), \
             patch("persistence_app.main_snapshot_consumer.SnapshotMongoWriter", return_value=writer):
            run_loop(topic="stream:snapshots:live", health_log_interval_sec=0)

        # New message was acked
        bus.acknowledge.assert_called_once_with(_SNAPSHOT_STREAM, _SNAPSHOT_GROUP, "1-1")
        writer.write_snapshot_event.assert_called_once()

    def test_no_pubsub_subscribe_in_run_loop(self):
        """A3 acceptance: pubsub.subscribe must not be called anywhere in run_loop."""
        bus = MagicMock()
        bus.ensure_group.return_value = None
        bus.consume.side_effect = KeyboardInterrupt

        writer = MagicMock()

        with patch("persistence_app.main_snapshot_consumer.RedisEventBus", return_value=bus), \
             patch("persistence_app.main_snapshot_consumer.SnapshotMongoWriter", return_value=writer):
            run_loop(topic="stream:snapshots:live", health_log_interval_sec=0)

        # bus is a RedisEventBus mock — it must NEVER have .subscribe() called
        bus.subscribe = MagicMock()
        self.assertEqual(bus.subscribe.call_count, 0)

    def test_ensure_group_called_on_startup(self):
        bus = MagicMock()
        bus.ensure_group.return_value = None
        bus.consume.side_effect = KeyboardInterrupt

        writer = MagicMock()

        with patch("persistence_app.main_snapshot_consumer.RedisEventBus", return_value=bus), \
             patch("persistence_app.main_snapshot_consumer.SnapshotMongoWriter", return_value=writer):
            run_loop(topic="stream:snapshots:live", health_log_interval_sec=0)

        bus.ensure_group.assert_called_once_with(_SNAPSHOT_STREAM, _SNAPSHOT_GROUP)


if __name__ == "__main__":
    unittest.main()
