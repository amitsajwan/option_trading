import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import joblib
import pandas as pd

from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.engines.ml_entry_policy import MLEntryPolicy
from strategy_app.engines.ml_regime_engine import MLRegimeEngine
from strategy_app.logging.signal_logger import SignalLogger
from strategy_app.main import (
    _enforce_ml_runtime_guard,
    _resolve_ml_entry_config,
    _resolve_ml_entry_threshold_policy,
    build_engine,
)


class MLEntryRuntimeTests(unittest.TestCase):
    def _write_bundle(self, root: Path) -> Path:
        bundle_path = root / "entry_quality_segmented_bundle.joblib"
        joblib.dump({"segments": {}}, bundle_path)
        return bundle_path

    def _write_summary(self, root: Path, *, bundle_path: Path, threshold_policy_id: str = "fixed_060") -> Path:
        summary_path = root / "summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "experiment_id": "winner",
                    "bundle_path": str(bundle_path),
                    "threshold_policy_id": threshold_policy_id,
                }
            ),
            encoding="utf-8",
        )
        return summary_path

    def test_from_registry_loads_fixed_threshold_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_path = self._write_bundle(root)
            registry_path = root / "experiment_registry.csv"
            pd.DataFrame(
                [
                    {
                        "status": "trained",
                        "experiment_id": "winner",
                        "bundle_path": str(bundle_path),
                        "threshold_policy_id": "fixed_060",
                    }
                ]
            ).to_csv(registry_path, index=False)

            policy = MLEntryPolicy.from_registry(registry_path=registry_path, experiment_id="winner")

            self.assertAlmostEqual(policy._default_threshold or 0.0, 0.60, places=6)
            self.assertEqual(policy._strategy_threshold_overrides, {})

    def test_from_registry_loads_strategy_override_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_path = self._write_bundle(root)
            registry_path = root / "evaluation_registry.csv"
            pd.DataFrame(
                [
                    {
                        "experiment_id": "winner",
                        "bundle_path": str(bundle_path),
                        "threshold_policy_id": "strategy_override_v1",
                    }
                ]
            ).to_csv(registry_path, index=False)

            policy = MLEntryPolicy.from_registry(registry_path=registry_path, experiment_id="winner")

            self.assertEqual(policy._default_threshold, None)
            self.assertAlmostEqual(policy._strategy_threshold_overrides["OI_BUILDUP"], 0.62, places=6)
            self.assertAlmostEqual(
                policy._strategy_regime_threshold_overrides[("SIDEWAYS", "OI_BUILDUP")],
                0.70,
                places=6,
            )

    def test_from_registry_supports_evaluation_registry_rows_via_summary_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_path = self._write_bundle(root)
            summary_path = self._write_summary(root, bundle_path=bundle_path, threshold_policy_id="fixed_060")
            registry_path = root / "evaluation_registry.csv"
            pd.DataFrame(
                [
                    {
                        "experiment_id": "winner",
                        "threshold_policy_id": "fixed_060",
                        "summary_json": str(summary_path),
                    }
                ]
            ).to_csv(registry_path, index=False)

            policy = MLEntryPolicy.from_registry(registry_path=registry_path, experiment_id="winner")

            self.assertAlmostEqual(policy._default_threshold or 0.0, 0.60, places=6)

    def test_from_registry_threshold_policy_override_supports_custom_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_path = self._write_bundle(root)
            registry_path = root / "experiment_registry.csv"
            pd.DataFrame(
                [
                    {
                        "status": "trained",
                        "experiment_id": "winner",
                        "bundle_path": str(bundle_path),
                        "threshold_policy_id": "fixed_060",
                    }
                ]
            ).to_csv(registry_path, index=False)

            policy = MLEntryPolicy.from_registry(
                registry_path=registry_path,
                experiment_id="winner",
                threshold_policy_override="fixed_custom_062",
            )
            self.assertAlmostEqual(policy._default_threshold or 0.0, 0.62, places=6)

    def test_from_registry_rejects_non_trained_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_path = self._write_bundle(root)
            registry_path = root / "experiment_registry.csv"
            pd.DataFrame(
                [
                    {
                        "status": "failed",
                        "experiment_id": "winner",
                        "bundle_path": str(bundle_path),
                        "threshold_policy_id": "fixed_060",
                    }
                ]
            ).to_csv(registry_path, index=False)

            with self.assertRaisesRegex(ValueError, "not deployable"):
                MLEntryPolicy.from_registry(registry_path=registry_path, experiment_id="winner")

    def test_build_engine_requires_registry_and_experiment_id_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = SignalLogger(Path(tmpdir))
            with self.assertRaisesRegex(ValueError, "requires both"):
                build_engine(
                    engine_name="deterministic",
                    min_confidence=0.65,
                    signal_logger=logger,
                    ml_entry_registry="registry.csv",
                )

    def test_resolve_ml_entry_config_uses_env_when_cli_missing(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ML_ENTRY_REGISTRY": "registry.csv",
                "ML_ENTRY_EXPERIMENT_ID": "winner",
            },
            clear=False,
        ):
            registry, experiment_id, source = _resolve_ml_entry_config(cli_registry=None, cli_experiment_id=None)

        self.assertEqual(registry, "registry.csv")
        self.assertEqual(experiment_id, "winner")
        self.assertEqual(source, "env")

    def test_resolve_ml_entry_config_cli_takes_precedence_over_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ML_ENTRY_REGISTRY": "env_registry.csv",
                "ML_ENTRY_EXPERIMENT_ID": "env-winner",
            },
            clear=False,
        ):
            registry, experiment_id, source = _resolve_ml_entry_config(
                cli_registry="cli_registry.csv",
                cli_experiment_id="cli-winner",
            )

        self.assertEqual(registry, "cli_registry.csv")
        self.assertEqual(experiment_id, "cli-winner")
        self.assertEqual(source, "cli")

    def test_resolve_ml_entry_config_rejects_partial_env_configuration(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ML_ENTRY_REGISTRY": "registry.csv",
                "ML_ENTRY_EXPERIMENT_ID": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "requires both"):
                _resolve_ml_entry_config(cli_registry=None, cli_experiment_id=None)

    def test_resolve_ml_entry_config_disables_when_empty(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ML_ENTRY_REGISTRY": "",
                "ML_ENTRY_EXPERIMENT_ID": "",
            },
            clear=False,
        ):
            registry, experiment_id, source = _resolve_ml_entry_config(cli_registry=None, cli_experiment_id=None)

        self.assertIsNone(registry)
        self.assertIsNone(experiment_id)
        self.assertEqual(source, "disabled")

    def test_resolve_ml_entry_threshold_policy_prefers_cli_then_env(self) -> None:
        with patch.dict("os.environ", {"ML_ENTRY_THRESHOLD_POLICY": "fixed_065"}, clear=False):
            self.assertEqual(_resolve_ml_entry_threshold_policy("fixed_custom_062"), "fixed_custom_062")
            self.assertEqual(_resolve_ml_entry_threshold_policy(None), "fixed_065")

    def test_build_engine_injects_registry_backed_entry_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_path = self._write_bundle(root)
            registry_path = root / "experiment_registry.csv"
            pd.DataFrame(
                [
                    {
                        "status": "trained",
                        "experiment_id": "winner",
                        "bundle_path": str(bundle_path),
                        "threshold_policy_id": "fixed_060",
                    }
                ]
            ).to_csv(registry_path, index=False)

            engine = build_engine(
                engine_name="deterministic",
                min_confidence=0.65,
                signal_logger=SignalLogger(root),
                ml_entry_registry=str(registry_path),
                ml_entry_experiment_id="winner",
            )

            self.assertIsInstance(engine, DeterministicRuleEngine)
            self.assertIsInstance(engine._injected_entry_policy, MLEntryPolicy)

    def test_build_engine_applies_threshold_override_to_entry_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_path = self._write_bundle(root)
            registry_path = root / "experiment_registry.csv"
            pd.DataFrame(
                [
                    {
                        "status": "trained",
                        "experiment_id": "winner",
                        "bundle_path": str(bundle_path),
                        "threshold_policy_id": "fixed_060",
                    }
                ]
            ).to_csv(registry_path, index=False)

            engine = build_engine(
                engine_name="deterministic",
                min_confidence=0.65,
                signal_logger=SignalLogger(root),
                ml_entry_registry=str(registry_path),
                ml_entry_experiment_id="winner",
                ml_entry_threshold_policy="fixed_custom_062",
            )
            self.assertIsInstance(engine, DeterministicRuleEngine)
            self.assertIsNotNone(engine._injected_entry_policy)
            self.assertAlmostEqual(engine._injected_entry_policy._default_threshold or 0.0, 0.62, places=6)

    def test_build_engine_supports_ml_regime_delegate_with_registry_backed_entry_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_path = self._write_bundle(root)
            registry_path = root / "experiment_registry.csv"
            pd.DataFrame(
                [
                    {
                        "status": "trained",
                        "experiment_id": "winner",
                        "bundle_path": str(bundle_path),
                        "threshold_policy_id": "fixed_060",
                    }
                ]
            ).to_csv(registry_path, index=False)

            engine = build_engine(
                engine_name="ml",
                min_confidence=0.65,
                signal_logger=SignalLogger(root),
                ml_entry_registry=str(registry_path),
                ml_entry_experiment_id="winner",
            )

            self.assertIsInstance(engine, MLRegimeEngine)
            self.assertIsInstance(engine._delegate, DeterministicRuleEngine)
            self.assertIsInstance(engine._delegate._injected_entry_policy, MLEntryPolicy)

    def test_ml_runtime_guard_requires_guard_file_when_ml_enabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires --ml-runtime-guard-file"):
            _enforce_ml_runtime_guard(
                experiment_id="winner",
                registry_path="registry.csv",
                rollout_stage="capped_live",
                position_size_multiplier=0.25,
                guard_file=None,
            )

    def test_ml_runtime_guard_rejects_non_capped_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            guard = Path(tmpdir) / "guard.json"
            guard.write_text(
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
            with self.assertRaisesRegex(ValueError, "only in capped_live"):
                _enforce_ml_runtime_guard(
                    experiment_id="winner",
                    registry_path="registry.csv",
                    rollout_stage="paper",
                    position_size_multiplier=0.25,
                    guard_file=str(guard),
                )

    def test_ml_runtime_guard_accepts_valid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            guard = Path(tmpdir) / "guard.json"
            guard.write_text(
                json.dumps(
                    {
                        "approved_for_runtime": True,
                        "offline_strict_positive_passed": True,
                        "paper_days_observed": 10,
                        "shadow_days_observed": 10,
                        "approved_experiment_id": "winner",
                    }
                ),
                encoding="utf-8",
            )
            _enforce_ml_runtime_guard(
                experiment_id="winner",
                registry_path="registry.csv",
                rollout_stage="capped_live",
                position_size_multiplier=0.25,
                guard_file=str(guard),
            )


if __name__ == "__main__":
    unittest.main()
