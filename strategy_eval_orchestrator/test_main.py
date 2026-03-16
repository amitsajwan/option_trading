import unittest
from pathlib import Path
from unittest.mock import patch

from snapshot_app.historical.snapshot_access import DEFAULT_HISTORICAL_PARQUET_BASE
from strategy_eval_orchestrator.main import _default_snapshot_parquet_base, validate_rollout_command


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


if __name__ == "__main__":
    unittest.main()
