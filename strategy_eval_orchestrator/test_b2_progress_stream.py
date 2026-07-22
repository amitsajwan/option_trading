"""Tests for B2 — eval run progress events written to Redis Stream.

Verifies:
  - _publish_run_event XADDs to stream:eval:progress:{run_id}
  - pub/sub PUBLISH is still called (WebSocket bridge depends on it)
  - stream entry contains 'payload' (JSON) and 'event_type' fields
  - MAXLEN=200 passed to xadd
  - _progress_stream uses correct prefix and env override
  - dashboard get_run_progress_history reads from xrange
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch
import os

from strategy_eval_orchestrator.main import (
    _progress_stream,
    _PROGRESS_STREAM_MAXLEN,
    _publish_run_event,
)


class TestProgressStreamHelper(unittest.TestCase):
    def test_default_prefix(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STRATEGY_EVAL_PROGRESS_STREAM_PREFIX", None)
            name = _progress_stream("abc-123")
            self.assertEqual(name, "stream:eval:progress:abc-123")

    def test_env_prefix_override(self):
        with patch.dict(os.environ, {"STRATEGY_EVAL_PROGRESS_STREAM_PREFIX": "test:progress:"}):
            self.assertEqual(_progress_stream("run-1"), "test:progress:run-1")

    def test_maxlen_is_200(self):
        self.assertEqual(_PROGRESS_STREAM_MAXLEN, 200)


class TestPublishRunEvent(unittest.TestCase):
    def _make_client(self):
        return MagicMock()

    def test_xadd_called_with_correct_stream(self):
        client = self._make_client()
        _publish_run_event(client, "run-42", {"event_type": "run_started", "progress_pct": 0.0})
        client.xadd.assert_called_once()
        args, kwargs = client.xadd.call_args
        self.assertEqual(args[0], "stream:eval:progress:run-42")

    def test_xadd_entry_has_payload_and_event_type(self):
        client = self._make_client()
        _publish_run_event(client, "run-42", {"event_type": "run_progress", "progress_pct": 50.0})
        args, kwargs = client.xadd.call_args
        entry = args[1]
        self.assertIn("payload", entry)
        self.assertEqual(entry["event_type"], "run_progress")
        body = json.loads(entry["payload"])
        self.assertEqual(body["event_type"], "run_progress")
        self.assertEqual(body["run_id"], "run-42")

    def test_xadd_maxlen_is_200_approximate(self):
        client = self._make_client()
        _publish_run_event(client, "run-99", {"event_type": "run_completed"})
        _, kwargs = client.xadd.call_args
        self.assertEqual(kwargs["maxlen"], 200)
        self.assertTrue(kwargs["approximate"])

    def test_pubsub_publish_still_called_for_websocket(self):
        client = self._make_client()
        _publish_run_event(client, "run-42", {"event_type": "run_started"})
        publish_calls = [c[0][0] for c in client.publish.call_args_list]
        self.assertIn("strategy:eval:run:run-42", publish_calls)
        self.assertIn("strategy:eval:global", publish_calls)

    def test_run_id_and_timestamp_injected(self):
        client = self._make_client()
        _publish_run_event(client, "my-run", {"event_type": "run_queued"})
        args, _ = client.xadd.call_args
        body = json.loads(args[1]["payload"])
        self.assertEqual(body["run_id"], "my-run")
        self.assertIn("timestamp", body)


class TestDashboardProgressHistory(unittest.TestCase):
    def _make_service(self):
        from market_data_dashboard.services.strategy_evaluation_service import StrategyEvaluationService
        svc = StrategyEvaluationService()
        redis_mock = MagicMock()
        svc._redis_client = redis_mock
        svc._indexes_ready = True
        return svc, redis_mock

    def test_get_run_progress_history_reads_xrange(self):
        svc, redis_mock = self._make_service()
        event_body = {"run_id": "r1", "event_type": "run_progress", "progress_pct": 25.0}
        redis_mock.xrange.return_value = [
            ("1234-0", {"payload": json.dumps(event_body), "event_type": "run_progress"}),
        ]
        result = svc.get_run_progress_history("r1")
        redis_mock.xrange.assert_called_once_with("stream:eval:progress:r1", count=200)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event_type"], "run_progress")
        self.assertAlmostEqual(result[0]["progress_pct"], 25.0)

    def test_get_run_progress_history_returns_empty_on_redis_error(self):
        svc, redis_mock = self._make_service()
        redis_mock.xrange.side_effect = Exception("connection refused")
        result = svc.get_run_progress_history("r1")
        self.assertEqual(result, [])

    def test_get_run_progress_history_skips_invalid_json(self):
        svc, redis_mock = self._make_service()
        redis_mock.xrange.return_value = [
            ("1111-0", {"payload": "not-json", "event_type": "x"}),
            ("2222-0", {"payload": json.dumps({"event_type": "run_completed"}), "event_type": "run_completed"}),
        ]
        result = svc.get_run_progress_history("r1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event_type"], "run_completed")

    def test_progress_stream_name_uses_run_id(self):
        svc, _ = self._make_service()
        self.assertEqual(svc._progress_stream("abc"), "stream:eval:progress:abc")


class TestProgressStreamExpiry(unittest.TestCase):
    def test_ttl_constant_is_24h(self):
        from strategy_eval_orchestrator.main import _PROGRESS_STREAM_TTL_SECS
        self.assertEqual(_PROGRESS_STREAM_TTL_SECS, 86400)

    def test_expire_called_on_stream_key(self):
        from strategy_eval_orchestrator.main import _expire_progress_stream
        client = MagicMock()
        _expire_progress_stream(client, "run-77")
        client.expire.assert_called_once_with("stream:eval:progress:run-77", 86400)

    def test_expire_swallows_redis_errors(self):
        from strategy_eval_orchestrator.main import _expire_progress_stream
        client = MagicMock()
        client.expire.side_effect = Exception("connection lost")
        _expire_progress_stream(client, "run-77")  # must not raise


if __name__ == "__main__":
    unittest.main()
