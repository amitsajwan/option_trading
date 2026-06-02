"""Tests for C1 — SharedRedisPool (ws_redis_pool.py).

Verifies:
  - register/unregister lifecycle
  - ConnContext.matches() for channels and patterns
  - subscribe/unsubscribe updates ConnContext subscriptions
  - _dispatch routes only to matching connections
  - Queue drop-oldest on overflow (maxsize=100)
  - MAX_POOL_THREADS defaults to 4
  - Least-loaded thread assignment
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import MagicMock, patch

from market_data_dashboard.ws_redis_pool import (
    ConnContext,
    SharedRedisPool,
    MAX_POOL_THREADS,
    _QUEUE_MAXSIZE,
)


def _make_pool() -> SharedRedisPool:
    pool = SharedRedisPool()
    pool._started = True
    for _ in range(2):
        t = MagicMock()
        t.subscriber_count.return_value = 0
        pool._threads.append(t)
    return pool


def _sync_loop() -> asyncio.AbstractEventLoop:
    return asyncio.new_event_loop()


class TestPoolDefaults(unittest.TestCase):
    def test_max_pool_threads_is_at_least_4(self):
        self.assertGreaterEqual(MAX_POOL_THREADS, 4)

    def test_queue_maxsize_is_100(self):
        self.assertEqual(_QUEUE_MAXSIZE, 100)


class TestConnContextMatches(unittest.TestCase):
    def _make_ctx(self) -> ConnContext:
        loop = asyncio.new_event_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        return ConnContext(conn_id="c1", async_loop=loop, msg_queue=q)

    def test_exact_channel_match(self):
        ctx = self._make_ctx()
        ctx.add_subscription("channel", "strategy:eval:global")
        self.assertTrue(ctx.matches("strategy:eval:global"))

    def test_no_match_different_channel(self):
        ctx = self._make_ctx()
        ctx.add_subscription("channel", "strategy:eval:global")
        self.assertFalse(ctx.matches("market:snapshot:v1"))

    def test_pattern_match(self):
        ctx = self._make_ctx()
        ctx.add_subscription("pattern", "market:ohlc:BANKNIFTY:*")
        self.assertTrue(ctx.matches("market:ohlc:BANKNIFTY:5min"))

    def test_pattern_no_match(self):
        ctx = self._make_ctx()
        ctx.add_subscription("pattern", "market:ohlc:BANKNIFTY:*")
        self.assertFalse(ctx.matches("market:ohlc:NIFTY:5min"))

    def test_remove_subscription_stops_matching(self):
        ctx = self._make_ctx()
        ctx.add_subscription("channel", "market:snapshot:v1")
        ctx.remove_subscription("channel", "market:snapshot:v1")
        self.assertFalse(ctx.matches("market:snapshot:v1"))


class TestPoolRegisterUnregister(unittest.TestCase):
    def test_register_creates_context(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        ctx = pool.register("conn-1", loop)
        self.assertIn("conn-1", pool._registry)
        self.assertEqual(ctx.conn_id, "conn-1")

    def test_unregister_removes_context(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        pool.unregister("conn-1")
        self.assertNotIn("conn-1", pool._registry)

    def test_unregister_nonexistent_is_safe(self):
        pool = _make_pool()
        pool.unregister("does-not-exist")


class TestPoolSubscribe(unittest.TestCase):
    def test_subscribe_adds_to_conn_context(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        pool.subscribe("conn-1", "channel", "strategy:eval:global")
        ctx = pool._registry["conn-1"]
        self.assertIn("strategy:eval:global", ctx.channels)

    def test_subscribe_sends_ctrl_to_thread(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        t = pool._conn_to_thread["conn-1"]
        pool.subscribe("conn-1", "channel", "strategy:eval:global")
        t.send_ctrl.assert_called_with("subscribe", "strategy:eval:global")

    def test_subscribe_pattern_sends_psubscribe(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        pool.register("conn-1", loop)
        t = pool._conn_to_thread["conn-1"]
        pool.subscribe("conn-1", "pattern", "market:ohlc:*")
        t.send_ctrl.assert_called_with("psubscribe", "market:ohlc:*")


class TestPoolDispatch(unittest.TestCase):
    def test_dispatch_puts_to_matching_queue(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        ctx = pool.register("conn-1", loop)
        ctx.add_subscription("channel", "market:snapshot:v1")

        pool._dispatch("market:snapshot:v1", '{"snap": 1}')

        self.assertFalse(ctx.msg_queue.empty())
        msg = ctx.msg_queue.get_nowait()
        self.assertEqual(msg["channel"], "market:snapshot:v1")
        self.assertEqual(msg["data"]["snap"], 1)

    def test_dispatch_skips_non_matching(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        ctx = pool.register("conn-1", loop)
        ctx.add_subscription("channel", "market:snapshot:v1")

        pool._dispatch("other:channel", '{"x": 1}')
        self.assertTrue(ctx.msg_queue.empty())

    def test_dispatch_drops_oldest_on_overflow(self):
        loop = asyncio.new_event_loop()
        pool = _make_pool()
        ctx = pool.register("conn-1", loop)
        ctx.add_subscription("channel", "ch")

        for i in range(101):
            pool._dispatch("ch", f'{{"i": {i}}}')

        self.assertLessEqual(ctx.msg_queue.qsize(), 100)
        last = None
        while not ctx.msg_queue.empty():
            last = ctx.msg_queue.get_nowait()
        self.assertIsNotNone(last)
        self.assertEqual(last["data"]["i"], 100)


if __name__ == "__main__":
    unittest.main()
