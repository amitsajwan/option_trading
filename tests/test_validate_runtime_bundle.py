import json
import tempfile
import unittest
from pathlib import Path

from ops.gcp.validate_runtime_bundle import ValidationError, validate_runtime_bundle


def _write_env(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_guard(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "approved_for_runtime": True,
                "offline_strict_positive_passed": True,
                "paper_days_observed": 10,
                "shadow_days_observed": 10,
            }
        ),
        encoding="utf-8",
    )


class ValidateRuntimeBundleTests(unittest.TestCase):
    def test_runtime_validation_allows_run_id_with_threshold_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            env_file = repo_root / ".env.compose"
            guard_path = repo_root / "guards" / "runtime_guard.json"
            threshold_report = repo_root / "reports" / "thresholds.json"
            _write_guard(guard_path)
            threshold_report.parent.mkdir(parents=True, exist_ok=True)
            threshold_report.write_text("{}", encoding="utf-8")
            _write_env(
                env_file,
                {
                    "GHCR_IMAGE_PREFIX": "ghcr.io/amitsajwan",
                    "APP_IMAGE_TAG": "20260324-a1b2c3d",
                    "STRATEGY_ENGINE": "ml_pure",
                    "STRATEGY_ROLLOUT_STAGE": "capped_live",
                    "STRATEGY_POSITION_SIZE_MULTIPLIER": "0.25",
                    "STRATEGY_ML_RUNTIME_GUARD_FILE": "guards/runtime_guard.json",
                    "ML_PURE_RUN_ID": "20260323_123000",
                    "ML_PURE_MODEL_GROUP": "banknifty_futures/h15_tp_auto",
                    "ML_PURE_THRESHOLD_REPORT": "reports/thresholds.json",
                },
            )

            result = validate_runtime_bundle(mode="runtime", repo_root=repo_root, env_file=env_file)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.details["run_id"], "20260323_123000")
            self.assertEqual(result.details["threshold_report"], "reports/thresholds.json")
            self.assertIn("threshold report anchor preserved for run-id mode", result.checks)

    def test_runtime_validation_rejects_non_positive_size_multiplier(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            env_file = repo_root / ".env.compose"
            guard_path = repo_root / "guards" / "runtime_guard.json"
            _write_guard(guard_path)
            _write_env(
                env_file,
                {
                    "GHCR_IMAGE_PREFIX": "ghcr.io/amitsajwan",
                    "APP_IMAGE_TAG": "20260324-a1b2c3d",
                    "STRATEGY_ENGINE": "ml_pure",
                    "STRATEGY_ROLLOUT_STAGE": "capped_live",
                    "STRATEGY_POSITION_SIZE_MULTIPLIER": "0",
                    "STRATEGY_ML_RUNTIME_GUARD_FILE": "guards/runtime_guard.json",
                    "ML_PURE_RUN_ID": "20260323_123000",
                    "ML_PURE_MODEL_GROUP": "banknifty_futures/h15_tp_auto",
                },
            )

            with self.assertRaisesRegex(ValidationError, "POSITION_SIZE_MULTIPLIER > 0"):
                validate_runtime_bundle(mode="runtime", repo_root=repo_root, env_file=env_file)


if __name__ == "__main__":
    unittest.main()
