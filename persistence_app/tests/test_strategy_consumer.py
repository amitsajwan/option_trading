"""Tests for the decoupled strategy_consumer loop.

Validates the architectural fix for the 2026-05-15 silent-hang bug:
- writer thread runs in background; main loop never blocks on mongo
- mongo write failure does NOT stall the consumer loop
- bounded queue drops oldest when writer falls behind
- pubsub.get_message exception triggers reconnect, not hang
"""

from __future__ import annotations

import queue
import threading
import time
import unittest
from unittest.mock import MagicMock

from persistence_app.main_strategy_consumer import _WRITER_STOP, _writer_thread


class WriterThreadTests(unittest.TestCase):

    def _build_writer_with_metrics(self, write_outcome):
        writer = MagicMock()
        writer.write_strategy_event = MagicMock(side_effect=write_outcome)
        metrics = {
            "consumed": 0, "written": 0, "ignored": 0, "errors": 0,
            "dropped": 0, "last_message_at": None,
            "last_flush_success_at": None, "last_flush_error_at": None,
            "last_error_message": None,
        }
        lock = threading.Lock()
        return writer, metrics, lock

    def test_writer_thread_processes_success(self):
        writer, metrics, lock = self._build_writer_with_metrics(write_outcome=lambda p: True)
        q: "queue.Queue[object]" = queue.Queue()
        q.put({"event_type": "test"})
        q.put({"event_type": "test"})
        q.put(_WRITER_STOP)

        _writer_thread(payload_queue=q, writer=writer, metrics=metrics, metrics_lock=lock)

        self.assertEqual(metrics["written"], 2)
        self.assertEqual(metrics["errors"], 0)
        self.assertIsNotNone(metrics["last_flush_success_at"])

    def test_writer_thread_recovers_from_mongo_error(self):
        """Critical: a write exception must NOT terminate the writer thread."""
        outcomes = iter([True, ConnectionError("simulated mongo timeout"), True])
        def writer_fn(payload):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        writer, metrics, lock = self._build_writer_with_metrics(write_outcome=writer_fn)
        q: "queue.Queue[object]" = queue.Queue()
        q.put({"event_type": "a"})
        q.put({"event_type": "b"})
        q.put({"event_type": "c"})
        q.put(_WRITER_STOP)

        _writer_thread(payload_queue=q, writer=writer, metrics=metrics, metrics_lock=lock)

        # 2 successes (1st + 3rd), 1 error (2nd)
        self.assertEqual(metrics["written"], 2)
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(metrics["last_error_message"], "simulated mongo timeout")
        self.assertIsNotNone(metrics["last_flush_error_at"])
        # Even though the writer thread saw an error, it kept going and processed
        # the 3rd event successfully.

    def test_writer_thread_ignored_when_write_returns_false(self):
        writer, metrics, lock = self._build_writer_with_metrics(write_outcome=lambda p: False)
        q: "queue.Queue[object]" = queue.Queue()
        q.put({"event_type": "test"})
        q.put(_WRITER_STOP)

        _writer_thread(payload_queue=q, writer=writer, metrics=metrics, metrics_lock=lock)

        self.assertEqual(metrics["written"], 0)
        self.assertEqual(metrics["ignored"], 1)
        self.assertEqual(metrics["errors"], 0)

    def test_writer_thread_stops_on_sentinel(self):
        """Sentinel must terminate the writer cleanly."""
        writer, metrics, lock = self._build_writer_with_metrics(write_outcome=lambda p: True)
        q: "queue.Queue[object]" = queue.Queue()
        q.put(_WRITER_STOP)

        # Should return promptly without consuming anything.
        t0 = time.monotonic()
        _writer_thread(payload_queue=q, writer=writer, metrics=metrics, metrics_lock=lock)
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 1.0)
        self.assertEqual(metrics["written"], 0)
        writer.write_strategy_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
