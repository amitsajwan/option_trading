"""RedisEventPublisher must self-heal after a publish failure, not go dark
for the rest of the process's life.

Regression test for the 2026-07-29 incident: BankNifty's live strategy_app
hit one transient publish failure and then silently stopped publishing
vote/signal/position/decision-trace events to persistence_app for 5+ days --
JSONL (canonical) logging was unaffected, but the Mongo/dashboard sync went
dark with no further error logged, and nothing ever re-enabled it because
the old code set `self._enabled = False` permanently on the first failure.
"""
from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock

from strategy_app.logging.redis_event_publisher import RedisEventPublisher

_LOGGER = logging.getLogger("test_redis_event_publisher_reconnect")


class _FlakyBus:
    """A bus whose publish() fails a fixed number of times, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls: list[tuple[str, dict]] = []

    def publish(self, topic: str, event: dict) -> None:
        self.calls.append((topic, event))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("boom")


class RedisEventPublisherReconnectTest(unittest.TestCase):
    def test_disabled_by_env_var_never_publishes(self) -> None:
        import os
        os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = "0"
        try:
            bus = MagicMock()
            pub = RedisEventPublisher(bus=bus, logger=_LOGGER)
            self.assertFalse(pub.enabled)
            pub.publish("topic", {"a": 1})
            bus.publish.assert_not_called()
        finally:
            os.environ.pop("STRATEGY_REDIS_PUBLISH_ENABLED", None)

    def test_single_failure_does_not_permanently_disable(self) -> None:
        """The core regression: one failed publish must not kill publishing
        for the rest of the process's life."""
        bus = _FlakyBus(fail_times=1)
        pub = RedisEventPublisher(bus=bus, logger=_LOGGER)
        self.assertTrue(pub.enabled)

        pub.publish("topic", {"a": 1})  # fails once, internally
        self.assertTrue(pub.enabled)  # still enabled -- not permanently killed

        # Force past the reconnect cooldown so the next publish actually retries.
        pub._last_reconnect_attempt = 0.0
        pub.publish("topic", {"a": 2})  # succeeds this time
        self.assertEqual(len(bus.calls), 2)

    def test_rate_limited_between_reconnect_attempts(self) -> None:
        """After a failure, repeated publishes within the cooldown window must
        not hammer the bus with reconnect attempts."""
        bus = _FlakyBus(fail_times=1)
        pub = RedisEventPublisher(bus=bus, logger=_LOGGER)
        pub.publish("topic", {"a": 1})  # fails -> bus set to None, cooldown starts
        self.assertEqual(len(bus.calls), 1)

        # Within the cooldown window: publish() must no-op, not attempt bus.publish again.
        pub.publish("topic", {"a": 2})
        pub.publish("topic", {"a": 3})
        self.assertEqual(len(bus.calls), 1)

    def test_recreates_bus_via_factory_not_reused_stale_instance(self) -> None:
        """On reconnect, a fresh bus must be obtained -- not the same dead
        instance retried in place (which would just fail again identically)."""
        first_bus = _FlakyBus(fail_times=1)
        pub = RedisEventPublisher(bus=first_bus, logger=_LOGGER)

        pub.publish("topic", {"a": 1})  # fails -> self._bus set to None
        self.assertIsNone(pub._bus)

        # For injected-bus mode, _new_bus() returns the same injected instance
        # (that's the documented contract for test injection) -- so the retry
        # naturally goes through the same object, but only after the cooldown.
        pub._last_reconnect_attempt = 0.0
        pub.publish("topic", {"a": 2})
        self.assertIs(pub._bus, first_bus)
        self.assertEqual(len(first_bus.calls), 2)


if __name__ == "__main__":
    unittest.main()
