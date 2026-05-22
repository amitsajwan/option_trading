from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import joblib
import numpy as np

import market_data_dashboard.app as dashboard_app


def _write_published_model(root: Path, *, model_group: str, profile_id: str) -> str:
    group_root = root / "ml_pipeline_2" / "artifacts" / "published_models"
    for part in model_group.split("/"):
        group_root /= part

    model_dir = group_root / "model"
    profile_dir = group_root / "config" / "profiles" / profile_id
    reports_dir = group_root / "reports" / "training"
    model_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    model_path = model_dir / "model.joblib"
    threshold_path = profile_dir / "threshold_report.json"
    training_path = profile_dir / "training_report.json"
    contract_path = group_root / "model_contract.json"
    latest_path = reports_dir / "latest.json"

    package = {
        "feature_columns": ["rsi_14", "ret_5m", "opt_0_ce_oi"],
        "models": {
            "ce": SimpleNamespace(coef_=np.asarray([[0.10, 0.55, 0.25]], dtype=float)),
            "pe": SimpleNamespace(coef_=np.asarray([[0.30, 0.20, 0.40]], dtype=float)),
        },
        "direction_semantics": {
            "ce": {"label": "CE"},
            "pe": {"label": "PE"},
        },
        "selected_feature_set": "legacy_snapshot_contract",
        "selected_model": {"name": "dummy_logreg", "family": "logreg"},
        "feature_profile": "legacy_snapshot_contract",
    }
    joblib.dump(package, model_path)

    threshold_path.write_text("{}", encoding="utf-8")
    training_path.write_text(
        json.dumps(
            {
                "best_experiment": {
                    "experiment_id": "exp_001",
                    "objective_value": 1.23,
                    "result": {
                        "ce": {
                            "folds": [
                                {
                                    "days": {
                                        "train_days": ["2024-01-01", "2024-01-10"],
                                        "valid_days": ["2024-01-15"],
                                        "test_days": ["2024-01-31"],
                                    }
                                }
                            ]
                        },
                        "pe": {
                            "folds": [
                                {
                                    "days": {
                                        "train_days": ["2024-01-02"],
                                        "valid_days": ["2024-01-20"],
                                        "test_days": ["2024-01-30"],
                                    }
                                }
                            ]
                        },
                    },
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    contract_path.write_text(json.dumps({"required_features": ["osc_rsi_14", "ret_5m"]}, indent=2), encoding="utf-8")
    latest_path.write_text(
        json.dumps(
            {
                "run_id": "run_20260317_010101",
                "model_group": model_group,
                "profile_id": profile_id,
                "feature_profile": "legacy_snapshot_contract",
                "published_paths": {
                    "model_package": str(model_path),
                    "threshold_report": str(threshold_path),
                    "training_report": str(training_path),
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return dashboard_app._normalize_trading_instance(model_group.replace("/", "_"))


class FeatureIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard_app._load_model_package_cached.cache_clear()

    def tearDown(self) -> None:
        dashboard_app._load_model_package_cached.cache_clear()

    def test_catalog_discovers_ready_artifact_models(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            model_a = _write_published_model(tmp_path, model_group="banknifty_futures/h15_tp_auto", profile_id="openfe_v9_dual")
            model_b = _write_published_model(tmp_path, model_group="core_v2/pureml_h15_4y_strict6", profile_id="openfe_v9_dual")

            with patch.object(dashboard_app, "REPO_ROOT", tmp_path), patch.object(
                dashboard_app,
                "TRADING_MODEL_CATALOG_DIR",
                tmp_path / "missing_catalog",
            ), patch.object(
                dashboard_app,
                "ML_PIPELINE_2_ARTIFACT_MODEL_CATALOG_DIR",
                tmp_path / "ml_pipeline_2" / "artifacts" / "published_models",
            ):
                catalog = dashboard_app._build_trading_model_catalog()

            ready = [entry for entry in catalog if entry.get("ready_to_run")]
            self.assertTrue(any(str(entry.get("source") or "").startswith("artifact_discovery") for entry in ready))
            instance_keys = {str(entry.get("instance_key") or "") for entry in ready}
            self.assertIn(model_a, instance_keys)
            self.assertIn(model_b, instance_keys)

    def test_feature_intelligence_maps_legacy_inputs_to_v1_names(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            model_key = _write_published_model(tmp_path, model_group="banknifty_futures/h15_tp_auto", profile_id="openfe_v9_dual")

            with patch.object(dashboard_app, "REPO_ROOT", tmp_path), patch.object(
                dashboard_app,
                "TRADING_MODEL_CATALOG_DIR",
                tmp_path / "missing_catalog",
            ), patch.object(
                dashboard_app,
                "ML_PIPELINE_2_ARTIFACT_MODEL_CATALOG_DIR",
                tmp_path / "ml_pipeline_2" / "artifacts" / "published_models",
            ):
                payload = asyncio.run(
                    dashboard_app.get_trading_feature_intelligence(
                        model=model_key,
                        date_from="2024-01-01",
                        date_to="2024-01-31",
                    )
                )

        self.assertEqual(payload["status"], "ok")
        ranking_names = [str(row.get("feature_name") or "") for row in payload["ranking"]["rows"]]
        self.assertIn("osc_rsi_14", ranking_names)
        self.assertNotIn("rsi_14", ranking_names)
        self.assertTrue(all(not name.startswith("opt_0_") for name in ranking_names))

        groups = {str(group.get("group_key") or ""): group for group in payload["groups"]}
        self.assertIn("osc", groups)
        self.assertTrue(
            any(
                feature.get("feature_name") == "osc_rsi_14" and feature.get("is_selected")
                for feature in groups["osc"]["features"]
            )
        )

    def test_feature_intelligence_returns_scatter_and_coverage_context(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            model_key = _write_published_model(tmp_path, model_group="banknifty_futures/h15_tp_auto", profile_id="openfe_v9_dual")

            with patch.object(dashboard_app, "REPO_ROOT", tmp_path), patch.object(
                dashboard_app,
                "TRADING_MODEL_CATALOG_DIR",
                tmp_path / "missing_catalog",
            ), patch.object(
                dashboard_app,
                "ML_PIPELINE_2_ARTIFACT_MODEL_CATALOG_DIR",
                tmp_path / "ml_pipeline_2" / "artifacts" / "published_models",
            ):
                payload = asyncio.run(
                    dashboard_app.get_trading_feature_intelligence(
                        model=model_key,
                        date_from="2024-01-01",
                        date_to="2024-01-31",
                    )
                )

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["summary"]["requested_range_in_coverage"])
        self.assertGreater(payload["summary"]["scatter_point_count"], 0)
        self.assertGreater(payload["summary"]["removed_legacy_feature_count"], 0)

        first_point = payload["scatter"]["points"][0]
        self.assertIn("feature_name", first_point)
        self.assertIn("x", first_point)
        self.assertIn("y", first_point)


if __name__ == "__main__":
    unittest.main()
