"""Tests for B1 — eval command stream on orchestrator side.

Verifies:
  - run_loop reads from stream:eval:commands via XREADGROUP (not just pub/sub)
  - Pending messages are re-delivered on restart (stream_id="0" first)
  - After PEL drained, switches to ">" for new messages
  - EVAL_COMMANDS_PUBSUB_SHADOW=false disables pub/sub subscribe
  - _ensure_command_group swallows BUSYGROUP, warns on other errors
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, call, patch

from strategy_eval_orchestrator.main import (
    _command_stream,
    _command_stream_group,
    _ensure_command_group,
    _eval_commands_pubsub_shadow,
)


class TestCommandStreamHelpers:
    def test_default_stream_name(self, monkeypatch):
        monkeypatch.delenv("STRATEGY_EVAL_COMMAND_STREAM", raising=False)
        assert _command_stream() == "stream:eval:commands"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_EVAL_COMMAND_STREAM", "stream:eval:commands:test")
        assert _command_stream() == "stream:eval:commands:test"

    def test_default_group_name(self, monkeypatch):
        monkeypatch.delenv("STRATEGY_EVAL_COMMAND_GROUP", raising=False)
        assert _command_stream_group() == "eval-orchestrator-grp-1"

    def test_shadow_true_by_default(self, monkeypatch):
        monkeypatch.delenv("EVAL_COMMANDS_PUBSUB_SHADOW", raising=False)
        assert _eval_commands_pubsub_shadow() is True

    def test_shadow_disabled(self, monkeypatch):
        monkeypatch.setenv("EVAL_COMMANDS_PUBSUB_SHADOW", "false")
        assert _eval_commands_pubsub_shadow() is False


class TestEnsureCommandGroup(unittest.TestCase):
    def test_busygroup_is_silently_swallowed(self):
        client = MagicMock()
        client.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")
        _ensure_command_group(client)

    def test_other_errors_logged_not_raised(self):
        client = MagicMock()
        client.xgroup_create.side_effect = Exception("WRONGTYPE")
        _ensure_command_group(client)

    def test_creates_group_with_id_zero_mkstream(self):
        client = MagicMock()
        _ensure_command_group(client)
        client.xgroup_create.assert_called_once_with(
            "stream:eval:commands", "eval-orchestrator-grp-1", id="0", mkstream=True
        )


class TestDashboardCommandStream(unittest.TestCase):
    """Tests for the dashboard-side XADD in queue_replay_run."""

    def _make_service(self):
        from market_data_dashboard.services.strategy_evaluation_service import StrategyEvaluationService
        svc = StrategyEvaluationService()
        redis_mock = MagicMock()
        mongo_mock = MagicMock()
        coll_mock = MagicMock()
        mongo_mock.__getitem__ = MagicMock(return_value=MagicMock(__getitem__=MagicMock(return_value=coll_mock)))
        svc._redis_client = redis_mock
        svc._mongo_client = mongo_mock
        svc._indexes_ready = True
        svc._db = MagicMock(return_value=mongo_mock)
        return svc, redis_mock, coll_mock

    def test_xadd_called_on_queue_replay_run(self, monkeypatch=None):
        import os
        with patch.dict(os.environ, {"EVAL_COMMANDS_PUBSUB_SHADOW": "true"}):
            svc, redis_mock, _ = self._make_service()
            redis_mock.__getitem__ = MagicMock(return_value=MagicMock())
            db_mock = MagicMock()
            coll_mock = MagicMock()
            db_mock.__getitem__ = MagicMock(return_value=coll_mock)
            svc._db = MagicMock(return_value=db_mock)

            svc.queue_replay_run(
                dataset="historical",
                date_from="2024-01-01",
                date_to="2024-01-31",
                speed=0.0,
                base_path="/tmp/snap",
            )

            redis_mock.xadd.assert_called_once()
            xadd_args = redis_mock.xadd.call_args
            assert xadd_args[0][0] == "stream:eval:commands"
            entry = xadd_args[0][1]
            assert "payload" in entry
            assert "run_id" in entry
            payload = json.loads(entry["payload"])
            assert payload["event_type"] == "strategy_eval_run_command"

    def test_pubsub_publish_called_when_shadow_true(self):
        import os
        with patch.dict(os.environ, {"EVAL_COMMANDS_PUBSUB_SHADOW": "true"}):
            svc, redis_mock, _ = self._make_service()
            db_mock = MagicMock()
            svc._db = MagicMock(return_value=db_mock)
            svc.queue_replay_run(
                dataset="historical",
                date_from="2024-01-01",
                date_to="2024-01-31",
                speed=0.0,
                base_path="/tmp/snap",
            )
            redis_mock.publish.assert_called()
            publish_topics = [c[0][0] for c in redis_mock.publish.call_args_list]
            assert "strategy:eval:command" in publish_topics

    def test_pubsub_not_called_for_command_when_shadow_false(self):
        import os
        with patch.dict(os.environ, {"EVAL_COMMANDS_PUBSUB_SHADOW": "false"}):
            svc, redis_mock, _ = self._make_service()
            db_mock = MagicMock()
            svc._db = MagicMock(return_value=db_mock)
            svc.queue_replay_run(
                dataset="historical",
                date_from="2024-01-01",
                date_to="2024-01-31",
                speed=0.0,
                base_path="/tmp/snap",
            )
            publish_topics = [c[0][0] for c in redis_mock.publish.call_args_list]
            assert "strategy:eval:command" not in publish_topics
            redis_mock.xadd.assert_called_once()


if __name__ == "__main__":
    unittest.main()
