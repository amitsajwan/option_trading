import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from ml_pipeline_2.publishing.resolver import resolve_ml_pure_artifacts, validate_switch_strict


class ModelSwitchResolverTests(unittest.TestCase):
    def _prepare_repo(self, root: Path, *, run_id: str, published: bool = True, create_artifacts: bool = True) -> Path:
        reports_dir = root / "ml_pipeline_2" / "artifacts" / "published_models" / "banknifty_futures" / "h15_tp_auto" / "reports" / "training"
        model_path = root / "ml_pipeline_2" / "artifacts" / "published_models" / "banknifty_futures" / "h15_tp_auto" / "data" / "training_runs" / run_id / "model" / "model.joblib"
        threshold_path = root / "ml_pipeline_2" / "artifacts" / "published_models" / "banknifty_futures" / "h15_tp_auto" / "data" / "training_runs" / run_id / "config" / "profiles" / "x" / "threshold_report.json"
        if create_artifacts:
            model_path.parent.mkdir(parents=True, exist_ok=True)
            threshold_path.parent.mkdir(parents=True, exist_ok=True)
            model_path.write_bytes(b"dummy")
            threshold_path.write_text("{}", encoding="utf-8")
        payload = {
            "run_id": run_id,
            "publish_status": "published" if published else "blocked",
            "publish_decision": {"decision": "PUBLISH" if published else "HOLD"},
            "published_paths": {
                "model_package": str(model_path.relative_to(root)).replace("\\", "/"),
                "threshold_report": str(threshold_path.relative_to(root)).replace("\\", "/"),
            },
        }
        run_path = reports_dir / f"run_{run_id}.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return run_path

    def test_resolve_ml_pure_artifacts_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._prepare_repo(root, run_id="20260308_164057", published=True, create_artifacts=True)
            with mock.patch.dict("os.environ", {"MODEL_SWITCH_REPO_ROOT": str(root)}, clear=False):
                resolved = resolve_ml_pure_artifacts("20260308_164057", "banknifty_futures/h15_tp_auto")
                self.assertTrue(Path(str(resolved["run_report_path"])).exists())
                self.assertTrue(Path(str(resolved["model_package_path"])).exists())
                self.assertTrue(Path(str(resolved["threshold_report_path"])).exists())

    def test_validate_switch_strict_blocks_non_published(self) -> None:
        payload = {
            "publish_status": "blocked",
            "publish_decision": {"decision": "HOLD"},
            "published_paths": {"model_package": "a", "threshold_report": "b"},
        }
        ok, reason = validate_switch_strict(payload)
        self.assertFalse(ok)
        self.assertIn("publish_status=blocked", reason)

    def test_resolve_ml_pure_artifacts_missing_run_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.dict("os.environ", {"MODEL_SWITCH_REPO_ROOT": str(root)}, clear=False):
                with self.assertRaisesRegex(FileNotFoundError, "run report not found"):
                    resolve_ml_pure_artifacts("20260308_999999", "banknifty_futures/h15_tp_auto")

    def test_validate_switch_strict_checks_missing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_path = self._prepare_repo(root, run_id="20260308_164057", published=True, create_artifacts=False)
            payload = json.loads(run_path.read_text(encoding="utf-8"))
            with mock.patch.dict("os.environ", {"MODEL_SWITCH_REPO_ROOT": str(root)}, clear=False):
                ok, reason = validate_switch_strict(payload)
                self.assertFalse(ok)
                self.assertIn("missing artifact", reason)


if __name__ == "__main__":
    unittest.main()
