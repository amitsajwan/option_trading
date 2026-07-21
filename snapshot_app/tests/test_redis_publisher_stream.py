"""Tests for A1 — RedisEventPublisher dual-write (Streams + pub/sub shadow).

Verifies:
  - Every publish XADDs to the correct stream
  - SNAPSHOT_PUBSUB_SHADOW=true  → also PUBLISHes to pub/sub
  - SNAPSHOT_PUBSUB_SHADOW=false → XADD only, no PUBLISH
  - Stream name routing: live vs historical topic
  - MAXLEN=500 is passed to xadd
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import pytest

from snapshot_app.redis_publisher import (
    RedisEventPublisher,
    _STREAM_MAXLEN,
    _stream_name_for_topic,
)


def _make_publisher() -> tuple[RedisEventPublisher, MagicMock]:
    client = MagicMock()
    return RedisEventPublisher(client=client), client


class TestStreamNameRouting:
    def test_live_topic_maps_to_live_stream(self):
        assert _stream_name_for_topic("market:snapshot:v1") == "stream:snapshots:live"

    def test_historical_topic_maps_to_historical_stream(self):
        assert _stream_name_for_topic("market:snapshot:v1:historical") == "stream:snapshots:historical"

    def test_any_historical_substring_routes_correctly(self):
        assert _stream_name_for_topic("market:snapshot:historical:test") == "stream:snapshots:historical"


class TestStreamOnlyPublish:
    """D2: shadow pub/sub removed. publish() is always XADD only."""

    def test_xadd_called_on_every_publish(self, monkeypatch):
        pub, client = _make_publisher()
        pub.publish(topic="market:snapshot:v1", payload={"snapshot_id": "s1"})
        client.xadd.assert_called_once()
        args, kwargs = client.xadd.call_args
        assert args[0] == "stream:snapshots:live"
        assert "payload" in args[1]
        assert kwargs["maxlen"] == _STREAM_MAXLEN
        assert kwargs["approximate"] is True

    def test_redis_publish_never_called(self, monkeypatch):
        pub, client = _make_publisher()
        pub.publish(topic="market:snapshot:v1", payload={"snapshot_id": "s1"})
        client.publish.assert_not_called()

    def test_payload_is_valid_json_in_stream_entry(self, monkeypatch):
        pub, client = _make_publisher()
        pub.publish(topic="market:snapshot:v1", payload={"key": "value", "num": 42})
        args, _ = client.xadd.call_args
        entry = args[1]
        parsed = json.loads(entry["payload"])
        assert parsed["key"] == "value"
        assert parsed["num"] == 42


class TestHistoricalTopicStream:
    def test_historical_publish_goes_to_historical_stream(self, monkeypatch):
        pub, client = _make_publisher()
        pub.publish(topic="market:snapshot:v1:historical", payload={"snapshot_id": "h1"})
        args, _ = client.xadd.call_args
        assert args[0] == "stream:snapshots:historical"

    def test_topic_stored_in_stream_entry(self, monkeypatch):
        pub, client = _make_publisher()
        pub.publish(topic="market:snapshot:v1:historical", payload={})
        args, _ = client.xadd.call_args
        assert args[1]["topic"] == "market:snapshot:v1:historical"
