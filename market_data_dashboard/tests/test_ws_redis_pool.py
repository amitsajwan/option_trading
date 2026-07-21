"""Tests for C1 — SharedRedisPool (ws_redis_pool.py).

Verifies:
  - register/unregister lifecycle; register() returns asyncio.Queue
  - subscribe/unsubscribe updates reverse indexes
  - shared Redis subscribe: ctrl_q gets one SUBSCRIBE for first listener only
  - shared Redis unsubscribe: ctrl_q gets UNSUBSCRIBE when last listener leaves
  - _dispatch routes only to matching connections (channel + pattern)
  - Queue drop-oldest on overflow (maxsize=100)
  - MAX_POOL_THREADS == 1 (single shared reader thread)
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock

from market_data_dashboard.ws_redis_pool import (
    SharedRedisPool,
    MAX_POOL_THREADS,
    _QUEUE_MAXSIZE,
)


def _make_pool() -> SharedRedisPool:
    """Return a pool with the reader thread suppressed (no real Redis needed)."""
    pool = SharedRedisPool()
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    pool._thread = mock_thread
    return pool


def _flush(loop: asyncio.AbstractEventLoop) -> None:
    loop.run_until_complete(asyncio.sleep(0))


class TestPoolDefaults(unittest.TestCase):
    def test_single_reader_thread(self):
        self.assertEqual(MAX_POOL_THREADS, 1)

    def test_queue_maxsize_is_100(self):
        self.assertEqual(_QUEUE_MAXSIZE, 100)


class TestPoolRegisterUnregister(unittest.TestCase):
    def test_register_returns_queue(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        q = pool.register("conn-1", loop)
        self.assertIsInstance(q, asyncio.Queue)
        self.assertIn("conn-1", pool._connections)

    def test_unregister_removes_connection(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        pool.unregister("conn-1")
        self.assertNotIn("conn-1", pool._connections)

    def test_unregister_nonexistent_is_safe(self):
        pool = _make_pool()
        pool.unregister("ghost")


class TestPoolSubscribe(unittest.TestCase):
    def _drain_ctrl(self, pool: SharedRedisPool) -> list[tuple]:
        items = []
        while True:
            try:
                items.append(pool._ctrl_q.get_nowait())
            except Exception:
                break
        return items

    def test_first_subscriber_sends_subscribe(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        pool.subscribe("conn-1", "channel", "strategy:eval:global")
        ctrl = self._drain_ctrl(pool)
        self.assertIn(("subscribe", "strategy:eval:global"), ctrl)

    def test_second_subscriber_does_not_resend_subscribe(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        pool.register("conn-2", loop)
        pool.subscribe("conn-1", "channel", "ch")
        self._drain_ctrl(pool)  # consume first subscribe
        pool.subscribe("conn-2", "channel", "ch")
        ctrl = self._drain_ctrl(pool)
        self.assertEqual(ctrl, [])  # no second SUBSCRIBE to Redis

    def test_last_unsubscribe_sends_unsubscribe(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        pool.subscribe("conn-1", "channel", "ch")
        self._drain_ctrl(pool)
        pool.unsubscribe("conn-1", "channel", "ch")
        ctrl = self._drain_ctrl(pool)
        self.assertIn(("unsubscribe", "ch"), ctrl)

    def test_non_last_unsubscribe_does_not_send_unsubscribe(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        pool.register("conn-2", loop)
        pool.subscribe("conn-1", "channel", "ch")
        pool.subscribe("conn-2", "channel", "ch")
        self._drain_ctrl(pool)
        pool.unsubscribe("conn-1", "channel", "ch")
        ctrl = self._drain_ctrl(pool)
        self.assertEqual(ctrl, [])  # conn-2 still listening — no UNSUBSCRIBE

    def test_pattern_subscribe_sends_psubscribe(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        pool.subscribe("conn-1", "pattern", "market:ohlc:*")
        ctrl = self._drain_ctrl(pool)
        self.assertIn(("psubscribe", "market:ohlc:*"), ctrl)

    def test_unregister_sends_unsubscribe_for_all_channels(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        pool.subscribe("conn-1", "channel", "ch-a")
        pool.subscribe("conn-1", "pattern", "pat:*")
        self._drain_ctrl(pool)
        pool.unregister("conn-1")
        ctrl = self._drain_ctrl(pool)
        self.assertIn(("unsubscribe", "ch-a"), ctrl)
        self.assertIn(("punsubscribe", "pat:*"), ctrl)


class TestPoolDispatch(unittest.TestCase):
    def test_dispatch_channel_puts_to_matching_queue(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        q = pool.register("conn-1", loop)
        pool.subscribe("conn-1", "channel", "market:snapshot:v1")

        pool._dispatch("market:snapshot:v1", '{"snap": 1}')
        _flush(loop)

        self.assertFalse(q.empty())
        msg = q.get_nowait()
        self.assertEqual(msg["channel"], "market:snapshot:v1")
        self.assertEqual(msg["data"]["snap"], 1)

    def test_dispatch_skips_non_matching(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        q = pool.register("conn-1", loop)
        pool.subscribe("conn-1", "channel", "market:snapshot:v1")

        pool._dispatch("other:channel", '{"x": 1}')
        _flush(loop)
        self.assertTrue(q.empty())

    def test_dispatch_pattern_routes_correctly(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        q = pool.register("conn-1", loop)
        pool.subscribe("conn-1", "pattern", "market:ohlc:BANKNIFTY:*")

        pool._dispatch("market:ohlc:BANKNIFTY:5min", '{"c": 99}')
        _flush(loop)

        self.assertFalse(q.empty())
        msg = q.get_nowait()
        self.assertEqual(msg["data"]["c"], 99)

    def test_dispatch_pattern_does_not_match_different_instrument(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        q = pool.register("conn-1", loop)
        pool.subscribe("conn-1", "pattern", "market:ohlc:BANKNIFTY:*")

        pool._dispatch("market:ohlc:NIFTY:5min", '{"c": 50}')
        _flush(loop)
        self.assertTrue(q.empty())

    def test_dispatch_drops_oldest_on_overflow(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        q = pool.register("conn-1", loop)
        pool.subscribe("conn-1", "channel", "ch")

        for i in range(101):
            pool._dispatch("ch", f'{{"i": {i}}}')
        _flush(loop)

        self.assertLessEqual(q.qsize(), 100)
        last = None
        while not q.empty():
            last = q.get_nowait()
        self.assertIsNotNone(last)
        self.assertEqual(last["data"]["i"], 100)

    def test_dispatch_only_reaches_subscribed_connections(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        q1 = pool.register("conn-1", loop)
        q2 = pool.register("conn-2", loop)
        pool.subscribe("conn-1", "channel", "ch-a")
        pool.subscribe("conn-2", "channel", "ch-b")

        pool._dispatch("ch-a", '{}')
        _flush(loop)

        self.assertFalse(q1.empty())
        self.assertTrue(q2.empty())


if __name__ == "__main__":
    unittest.main()
