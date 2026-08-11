import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from contracts_app import historical_snapshot_topic, stream_name_for_topic
from snapshot_app.historical.snapshot_access import DEFAULT_HISTORICAL_PARQUET_BASE
from strategy_eval_orchestrator.main import (
    _default_snapshot_parquet_base,
    _replay_and_publish,
    validate_rollout_command,
)


class OrchestratorRolloutValidationTests(unittest.TestCase):
    def test_invalid_stage_rejected(self) -> None:
        err = validate_rollout_command(
            rollout_stage="prod",
            paper_days_observed=0,
            shadow_days_observed=0,
            position_size_multiplier=1.0,
        )
        self.assertIsNotNone(err)
        self.assertIn("invalid rollout_stage", str(err))

    def test_shadow_requires_paper_days(self) -> None:
        err = validate_rollout_command(
            rollout_stage="shadow",
            paper_days_observed=9,
            shadow_days_observed=0,
            position_size_multiplier=1.0,
        )
        self.assertIsNotNone(err)
        self.assertIn("paper days", str(err))

    def test_capped_live_requires_shadow_days(self) -> None:
        err = validate_rollout_command(
            rollout_stage="capped_live",
            paper_days_observed=10,
            shadow_days_observed=9,
            position_size_multiplier=0.25,
        )
        self.assertIsNotNone(err)
        self.assertIn("shadow days", str(err))

    def test_capped_live_size_cap_enforced(self) -> None:
        err = validate_rollout_command(
            rollout_stage="capped_live",
            paper_days_observed=10,
            shadow_days_observed=10,
            position_size_multiplier=0.30,
        )
        self.assertIsNotNone(err)
        self.assertIn("position_size_multiplier", str(err))

    def test_paper_is_allowed(self) -> None:
        err = validate_rollout_command(
            rollout_stage="paper",
            paper_days_observed=0,
            shadow_days_observed=0,
            position_size_multiplier=1.0,
        )
        self.assertIsNone(err)

    def test_default_snapshot_parquet_base_uses_shared_historical_root(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_default_snapshot_parquet_base(), DEFAULT_HISTORICAL_PARQUET_BASE)

    def test_default_snapshot_parquet_base_honors_env_override(self) -> None:
        with patch.dict("os.environ", {"SNAPSHOT_PARQUET_BASE": "/tmp/snapshots"}, clear=False):
            self.assertEqual(_default_snapshot_parquet_base(), Path("/tmp/snapshots"))


class ReplayPublishTransportTests(unittest.TestCase):
    """Regression for the 2026-08-11 finding: _replay_and_publish used
    redis_client.publish() (pub/sub) while strategy_app_historical's
    consumer reads via XREADGROUP on a Streams key -- pub/sub messages with
    no live subscriber are lost, so every prior replay run through this
    endpoint silently fed zero events to the consumer despite reporting
    'emitted=N'. Must use XADD on the stream derived from the same topic,
    matching snapshot_app.redis_publisher.RedisEventPublisher's live wire
    format exactly (field names "payload"/"topic")."""

    def test_replay_uses_xadd_not_pubsub_publish(self) -> None:
        from snapshot_app.tests.test_market_snapshot_contract import _valid_snapshot

        snap = _valid_snapshot()
        frame = pd.DataFrame([{
            "timestamp": snap["timestamp"],
            "trade_date": snap["trade_date"],
            "snapshot_raw_json": json.dumps(snap),
        }])

        fake_store = MagicMock()
        fake_store.snapshots_for_date_range.return_value = frame
        redis_client = MagicMock()

        with patch("strategy_eval_orchestrator.main.require_snapshot_access", return_value=MagicMock(to_metadata=lambda: {})), \
             patch("strategy_eval_orchestrator.main.ParquetStore", return_value=fake_store):
            _replay_and_publish(
                redis_client=redis_client,
                run_id="test-run",
                date_from="2026-03-17",
                date_to="2026-03-17",
                base_path="/tmp/fake",
                speed=2400.0,
            )

        # xadd is also legitimately used for run-progress events (on a
        # separate stream:eval:progress:<run_id> stream) -- find the call
        # that targets the actual snapshot stream specifically.
        snapshot_stream = stream_name_for_topic(historical_snapshot_topic())
        snapshot_calls = [c for c in redis_client.xadd.call_args_list if c.args[0] == snapshot_stream]
        self.assertEqual(len(snapshot_calls), 1, f"expected exactly 1 xadd to {snapshot_stream}")
        fields = snapshot_calls[0].args[1]
        self.assertIn("payload", fields)
        self.assertEqual(json.loads(fields["payload"])["snapshot"]["snapshot_id"], snap["snapshot_id"])

        # publish() is still legitimately used for run-progress pub/sub
        # notifications, so this only asserts no snapshot payload was ever
        # sent via publish() (the original bug's exact shape).
        for call in redis_client.publish.call_args_list:
            self.assertNotIn(snap["snapshot_id"], str(call))


if __name__ == "__main__":
    unittest.main()
