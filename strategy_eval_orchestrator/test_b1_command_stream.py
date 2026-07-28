"""Tests for B1 — eval command stream on orchestrator side.

Verifies:
  - run_loop reads from stream:eval:commands via XREADGROUP
  - Pending messages are re-delivered on restart (stream_id="0" first)
  - After PEL drained, switches to ">" for new messages
  - run_loop does NOT also subscribe to/act on the pub/sub shadow channel
    (regression test — see run_loop()'s docstring comment: this orchestrator
    is the sole consumer of strategy:eval:command, and consuming both the
    stream and the pub/sub shadow used to run _process_command() twice,
    concurrently, for every single replay run submitted through the
    dashboard, since EVAL_COMMANDS_PUBSUB_SHADOW defaults to true. Found and
    fixed pre-merge; previously untested.)
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
    run_loop,
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


class TestRunLoopDoesNotDoubleConsume(unittest.TestCase):
    """Regression test for the duplicate-processing bug found pre-merge."""

    def test_pubsub_is_never_subscribed_or_read(self):
        redis_client = MagicMock()
        redis_client.xreadgroup.side_effect = KeyboardInterrupt  # end the loop after startup
        mongo_client = MagicMock()
        runs_coll = MagicMock()

        with patch.dict("os.environ", {"EVAL_COMMANDS_PUBSUB_SHADOW": "true"}), \
             patch("strategy_eval_orchestrator.main._redis_client", return_value=redis_client), \
             patch("strategy_eval_orchestrator.main._mongo_collection", return_value=(mongo_client, runs_coll)):
            run_loop()

        redis_client.pubsub.assert_not_called()

    def test_stream_command_processed_exactly_once(self):
        redis_client = MagicMock()
        payload = json.dumps({"event_type": "strategy_eval_run_command", "run_id": "r1"})
        batch = [("stream:eval:commands", [("1-1", {"payload": payload})])]
        redis_client.xreadgroup.side_effect = [batch, KeyboardInterrupt]
        mongo_client = MagicMock()
        runs_coll = MagicMock()

        with patch.dict("os.environ", {"EVAL_COMMANDS_PUBSUB_SHADOW": "true"}), \
             patch("strategy_eval_orchestrator.main._redis_client", return_value=redis_client), \
             patch("strategy_eval_orchestrator.main._mongo_collection", return_value=(mongo_client, runs_coll)), \
             patch("strategy_eval_orchestrator.main._process_command") as process_mock:
            run_loop()

        process_mock.assert_called_once()
        redis_client.xack.assert_called_once_with("stream:eval:commands", "eval-orchestrator-grp-1", "1-1")
        redis_client.pubsub.assert_not_called()

    def test_empty_pel_switches_to_blocking_reads_not_infinite_spin(self):
        """Regression test for the 2026-07-28 stuck-CPU incident.

        With id="0" (pending-entries read), real Redis/redis-py returns
        [[stream_name, []]] once the stream/group exists -- a non-empty outer
        list even when nothing is pending. A bare `not response` check never
        sees this as empty, so read_pending never flips to ">" and the loop
        spins forever without blocking (block= is ignored for id="0" reads).
        Found live: strategy_eval_orchestrator spun at ~78% CPU for 4+ days,
        last-delivered-id stuck at "0-0" despite lag=2 real messages waiting.
        """
        redis_client = MagicMock()
        empty_pel = [("stream:eval:commands", [])]
        redis_client.xreadgroup.side_effect = [empty_pel, KeyboardInterrupt]
        mongo_client = MagicMock()
        runs_coll = MagicMock()

        with patch.dict("os.environ", {"EVAL_COMMANDS_PUBSUB_SHADOW": "true"}), \
             patch("strategy_eval_orchestrator.main._redis_client", return_value=redis_client), \
             patch("strategy_eval_orchestrator.main._mongo_collection", return_value=(mongo_client, runs_coll)):
            run_loop()

        # First call must read the PEL (id="0"); second call must have
        # switched to ">" -- proving read_pending flipped instead of spinning.
        first_call, second_call = redis_client.xreadgroup.call_args_list
        assert first_call[0][2] == {"stream:eval:commands": "0"}
        assert second_call[0][2] == {"stream:eval:commands": ">"}


if __name__ == "__main__":
    unittest.main()
