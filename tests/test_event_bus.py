"""Tests for contracts_app.event_bus.RedisEventBus's socket_timeout floor.

Regression (found 2026-07-22, predates the streams-loose-coupling merge):
RedisEventBus.__init__ used kwargs.setdefault("socket_timeout", 30.0), but
redis_connection_kwargs(for_pubsub=False) already populates socket_timeout=2.0
unconditionally -- setdefault is a no-op against an already-present key, so
the intended 30s floor (needed because XREADGROUP calls block up to 5000ms)
never actually applied. Every default-constructed RedisEventBus() raced a 2s
client socket timeout against its own blocking reads -- a near-guaranteed
"Timeout reading from socket" on almost every idle poll. Caught live during
a NIFTY rehearsal deploy (persistence_app_nifty logging constant stream
read errors).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from contracts_app.event_bus import RedisEventBus


class TestSocketTimeoutFloor(unittest.TestCase):
    def test_default_construction_enforces_30s_floor(self) -> None:
        """redis_connection_kwargs() already sets socket_timeout=2.0 by
        default -- RedisEventBus must not let that value stand uncorrected."""
        with patch("redis.Redis") as redis_cls:
            RedisEventBus()
        _, kwargs = redis_cls.call_args
        self.assertGreaterEqual(kwargs["socket_timeout"], 30.0)

    def test_explicit_lower_override_still_gets_floored(self) -> None:
        with patch("redis.Redis") as redis_cls:
            RedisEventBus(redis_kwargs={"host": "localhost", "socket_timeout": 2.0})
        _, kwargs = redis_cls.call_args
        self.assertGreaterEqual(kwargs["socket_timeout"], 30.0)

    def test_explicit_higher_override_is_respected(self) -> None:
        with patch("redis.Redis") as redis_cls:
            RedisEventBus(redis_kwargs={"host": "localhost", "socket_timeout": 60.0})
        _, kwargs = redis_cls.call_args
        self.assertEqual(kwargs["socket_timeout"], 60.0)

    def test_socket_connect_timeout_also_floored(self) -> None:
        with patch("redis.Redis") as redis_cls:
            RedisEventBus()
        _, kwargs = redis_cls.call_args
        self.assertGreaterEqual(kwargs["socket_connect_timeout"], 5.0)


if __name__ == "__main__":
    unittest.main()
