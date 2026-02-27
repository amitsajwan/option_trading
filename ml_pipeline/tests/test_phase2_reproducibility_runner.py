import tempfile
import unittest
from pathlib import Path

from ml_pipeline.phase2_reproducibility_runner import (
    PHASE1_REQUIRED_ARTIFACTS,
    _ensure_required_artifacts,
    _phase2_commands,
    _resolve_phase1_artifacts_dir,
)


class Phase2ReproducibilityRunnerTests(unittest.TestCase):
    def _write_required_files(self, base_dir: Path) -> None:
        for name in PHASE1_REQUIRED_ARTIFACTS:
            path = base_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")

    def test_ensure_required_artifacts_raises_on_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with self.assertRaises(RuntimeError):
                _ensure_required_artifacts(base, PHASE1_REQUIRED_ARTIFACTS)

    def test_resolve_phase1_artifacts_dir_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            explicit = root / "phase1"
            explicit.mkdir(parents=True, exist_ok=True)
            self._write_required_files(explicit)

            resolved, meta = _resolve_phase1_artifacts_dir(
                base_path=root,
                root=root,
                workdir=root / "work",
                phase1_artifacts_dir=str(explicit),
                bootstrap_phase1=False,
            )

            self.assertEqual(resolved, explicit)
            self.assertEqual(meta["mode"], "explicit")
            self.assertEqual(meta["phase1_artifacts_dir"], str(explicit))

    def test_phase2_commands_reference_phase1_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            phase1 = root / "phase1"
            run_artifacts = root / "run1" / "artifacts"
            run_artifacts.mkdir(parents=True, exist_ok=True)

            commands = _phase2_commands(
                base_path=root,
                run_artifacts=run_artifacts,
                phase1_artifacts=phase1,
            )
            flat = " ".join(" ".join(cmd) for cmd in commands)
            self.assertIn(str(phase1 / "t04_features.parquet"), flat)
            self.assertIn(str(phase1 / "t06_baseline_model.joblib"), flat)
            self.assertIn(str(phase1 / "t08_threshold_report.json"), flat)
            self.assertIn(str(phase1 / "t11_paper_decisions.jsonl"), flat)


if __name__ == "__main__":
    unittest.main()
