import json
import tempfile
import unittest
from pathlib import Path

from ops.gcp.runtime_release_manifest import (
    CURRENT_MANIFEST_NAME,
    CURRENT_POINTER_NAME,
    CURRENT_RUNTIME_ENV_NAME,
    ReleaseManifestError,
    build_runtime_release_manifest,
)


class RuntimeReleaseManifestTests(unittest.TestCase):
    def test_build_runtime_release_manifest_writes_manifest_and_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            release_root = repo_root / "ml_pipeline_2" / "artifacts" / "research" / "run_123" / "release"
            release_root.mkdir(parents=True, exist_ok=True)
            runtime_env_path = release_root / "ml_pure_runtime.env"
            runtime_env_path.write_text(
                "STRATEGY_ENGINE=ml_pure\nML_PURE_RUN_ID=run_123\nML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto\n",
                encoding="utf-8",
            )
            training_release_json = repo_root / "training-release.json"
            payload = {
                "created_at_utc": "2026-03-24T12:00:00Z",
                "release_status": "published",
                "publish": {
                    "run_id": "run_123",
                    "model_group": "banknifty_futures/h15_tp_auto",
                    "profile_id": "openfe_v9_dual",
                    "active_group_paths": {
                        "threshold_report": "ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/threshold_report.json",
                        "training_report": "ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/training_report.json",
                    },
                },
                "paths": {
                    "runtime_env": str(runtime_env_path.resolve()),
                    "release_summary": str((release_root / "release_summary.json").resolve()),
                },
            }
            training_release_json.write_text(json.dumps(payload), encoding="utf-8")

            artifacts = build_runtime_release_manifest(
                training_release_path=training_release_json,
                repo_root=repo_root,
                app_image_tag="20260324-a1b2c3d",
                runtime_guard_path=".run/ml_runtime_guard_live.json",
                runtime_config_bucket_url="gs://runtime-config/runtime",
            )

            self.assertTrue(artifacts.manifest_path.exists())
            self.assertEqual(artifacts.manifest["run_id"], "run_123")
            self.assertEqual(artifacts.manifest["app_image_tag"], "20260324-a1b2c3d")
            self.assertEqual(
                artifacts.current_manifest_path.name,
                CURRENT_MANIFEST_NAME,
            )
            self.assertEqual(artifacts.current_pointer_path.name, CURRENT_POINTER_NAME)
            self.assertEqual(artifacts.current_runtime_env_path.name, CURRENT_RUNTIME_ENV_NAME)
            current_manifest = json.loads(artifacts.current_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(current_manifest["model_group"], "banknifty_futures/h15_tp_auto")
            current_pointer = json.loads(artifacts.current_pointer_path.read_text(encoding="utf-8"))
            self.assertEqual(current_pointer["run_id"], "run_123")
            self.assertIn("current_runtime_release.json", current_pointer["current_manifest_path"])

    def test_build_runtime_release_manifest_rejects_held_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            training_release_json = repo_root / "training-release.json"
            training_release_json.write_text(json.dumps({"release_status": "held"}), encoding="utf-8")

            with self.assertRaisesRegex(ReleaseManifestError, "not published"):
                build_runtime_release_manifest(
                    training_release_path=training_release_json,
                    repo_root=repo_root,
                    app_image_tag="latest",
                    runtime_guard_path=".run/ml_runtime_guard_live.json",
                )


if __name__ == "__main__":
    unittest.main()
