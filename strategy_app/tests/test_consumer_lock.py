"""Tests for Redis consumer lock (stable instance id + reclaim)."""

from __future__ import annotations

import os
import socket
import unittest
from threading import Event
from strategy_app.runtime.consumer_lock import (
    ConsumerLock,
    ConsumerLockOwner,
    build_lock_owner,
    owners_are_reclaimable,
)
from strategy_app.tests.test_redis_snapshot_consumer_dedupe import _FakeRedis


class ConsumerLockTests(unittest.TestCase):
    def test_owner_roundtrip_v2(self) -> None:
        owner = ConsumerLockOwner(
            instance_id="strategy_app_historical",
            host="deadcontainer",
            pid="1",
            token="abc",
            topic="market:snapshot:v1:historical",
        )
        text = owner.serialize()
        self.assertIn("|", text)
        parsed = ConsumerLockOwner.parse(text)
        self.assertEqual(parsed.instance_id, "strategy_app_historical")
        self.assertEqual(parsed.host, "deadcontainer")

    def test_reclaimable_same_instance_different_host(self) -> None:
        existing = ConsumerLockOwner(
            instance_id="strategy_app_historical",
            host="old_container_id",
            pid="9",
            token="dead",
            topic="market:snapshot:v1:historical",
        )
        ours = ConsumerLockOwner(
            instance_id="strategy_app_historical",
            host="new_container_id",
            pid="1",
            token="live",
            topic="market:snapshot:v1:historical",
        )
        self.assertTrue(owners_are_reclaimable(existing, ours))

    def test_reclaimed_after_force_recreate(self) -> None:
        os.environ["STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC"] = "5"
        os.environ["STRATEGY_CONSUMER_LOCK_INSTANCE_ID"] = "strategy_app_historical"
        try:
            client = _FakeRedis([])
            lock_key = "strategy_app:consumer_lock:market:snapshot:v1:historical"
            stale = ConsumerLockOwner(
                instance_id="strategy_app_historical",
                host="old_force_recreate_container",
                pid="99",
                token="stale01",
                topic="market:snapshot:v1:historical",
            ).serialize()
            client.set(lock_key, stale, nx=True, ex=120)

            lock = ConsumerLock(
                client,
                topic="market:snapshot:v1:historical",
                stop_event=Event(),
            )
            lock.acquire()
            self.assertEqual(client.get(lock_key), lock.owner)
            self.assertNotEqual(client.get(lock_key), stale)
        finally:
            os.environ.pop("STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC", None)
            os.environ.pop("STRATEGY_CONSUMER_LOCK_INSTANCE_ID", None)

    def test_same_hostname_still_reclaimable(self) -> None:
        os.environ["STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC"] = "5"
        try:
            client = _FakeRedis([])
            lock_key = "strategy_app:consumer_lock:market:snapshot:v1"
            host = socket.gethostname()
            stale = f"{host}:99999:dead0000:market:snapshot:v1"
            client.set(lock_key, stale, nx=True, ex=120)
            lock = ConsumerLock(client, topic="market:snapshot:v1", stop_event=Event())
            lock.acquire()
            self.assertEqual(client.get(lock_key), lock.owner)
        finally:
            os.environ.pop("STRATEGY_SINGLE_CONSUMER_LOCK_TTL_SEC", None)

    def test_build_lock_owner_uses_instance_env(self) -> None:
        os.environ["STRATEGY_CONSUMER_LOCK_INSTANCE_ID"] = "strategy_app_historical"
        try:
            owner = build_lock_owner("market:snapshot:v1:historical")
            self.assertEqual(owner.instance_id, "strategy_app_historical")
            self.assertIn("|", owner.serialize())
        finally:
            os.environ.pop("STRATEGY_CONSUMER_LOCK_INSTANCE_ID", None)


if __name__ == "__main__":
    unittest.main()
