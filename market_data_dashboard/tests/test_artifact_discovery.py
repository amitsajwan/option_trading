from __future__ import annotations

import json
from pathlib import Path

from market_data_dashboard import app as dashboard_app
from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_research
from ml_pipeline_2.tests.helpers import build_recovery_smoke_manifest, build_synthetic_feature_frames


def test_artifact_discovery_includes_ml_pipeline_2_published_models(tmp_path: Path, monkeypatch) -> None:
    group_root = tmp_path / "ml_pipeline_2" / "artifacts" / "published_models" / "banknifty_futures" / "h15_tp_auto"
    (group_root / "model").mkdir(parents=True, exist_ok=True)
    (group_root / "config" / "profiles" / "openfe_v9_dual").mkdir(parents=True, exist_ok=True)
    (group_root / "reports" / "training").mkdir(parents=True, exist_ok=True)
    (group_root / "model" / "model.joblib").write_bytes(b"dummy")
    (group_root / "config" / "profiles" / "openfe_v9_dual" / "threshold_report.json").write_text("{}", encoding="utf-8")
    (group_root / "config" / "profiles" / "openfe_v9_dual" / "training_report.json").write_text("{}", encoding="utf-8")
    (group_root / "model_contract.json").write_text(json.dumps({"required_features": ["ret_5m"]}), encoding="utf-8")
    (group_root / "reports" / "training" / "latest.json").write_text(
        json.dumps(
            {
                "run_id": "run_20260313_010101",
                "model_group": "banknifty_futures/h15_tp_auto",
                "profile_id": "openfe_v9_dual",
                "published_paths": {
                    "model_package": "ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/model/model.joblib",
                    "threshold_report": "ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/threshold_report.json",
                    "training_report": "ml_pipeline_2/artifacts/published_models/banknifty_futures/h15_tp_auto/config/profiles/openfe_v9_dual/training_report.json",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(dashboard_app, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dashboard_app, "ML_PIPELINE_2_ARTIFACT_MODEL_CATALOG_DIR", tmp_path / "ml_pipeline_2" / "artifacts" / "published_models")
    monkeypatch.setattr(dashboard_app, "ARTIFACT_MODEL_CATALOG_DIR", tmp_path / "ml_pipeline" / "artifacts" / "models" / "by_features")

    entries = dashboard_app._build_artifact_discovery_entries()

    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"] == "artifact_discovery_ml_pipeline_2"
    assert entry["model_group"] == "banknifty_futures/h15_tp_auto"
    assert entry["profile_id"] == "openfe_v9_dual"


def test_artifact_discovery_tolerates_non_dict_published_paths(tmp_path: Path, monkeypatch) -> None:
    group_root = tmp_path / "ml_pipeline_2" / "artifacts" / "published_models" / "banknifty_futures" / "h15_tp_auto"
    (group_root / "model").mkdir(parents=True, exist_ok=True)
    (group_root / "config" / "profiles" / "openfe_v9_dual").mkdir(parents=True, exist_ok=True)
    (group_root / "reports" / "training").mkdir(parents=True, exist_ok=True)
    (group_root / "model" / "model.joblib").write_bytes(b"dummy")
    (group_root / "config" / "profiles" / "openfe_v9_dual" / "threshold_report.json").write_text("{}", encoding="utf-8")
    (group_root / "config" / "profiles" / "openfe_v9_dual" / "training_report.json").write_text("{}", encoding="utf-8")
    (group_root / "model_contract.json").write_text(json.dumps({"required_features": ["ret_5m"]}), encoding="utf-8")
    (group_root / "reports" / "training" / "latest.json").write_text(
        json.dumps(
            {
                "run_id": "run_20260313_010101",
                "model_group": "banknifty_futures/h15_tp_auto",
                "profile_id": "openfe_v9_dual",
                "published_paths": "invalid-shape",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(dashboard_app, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dashboard_app, "ML_PIPELINE_2_ARTIFACT_MODEL_CATALOG_DIR", tmp_path / "ml_pipeline_2" / "artifacts" / "published_models")
    monkeypatch.setattr(dashboard_app, "ARTIFACT_MODEL_CATALOG_DIR", tmp_path / "ml_pipeline" / "artifacts" / "models" / "by_features")

    entries = dashboard_app._build_artifact_discovery_entries()

    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"] == "artifact_discovery_ml_pipeline_2"
    assert entry["exists"]["model_package"] is True
    assert entry["exists"]["threshold_report"] is True


def test_artifact_discovery_includes_recovery_research_models(tmp_path: Path, monkeypatch) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    recipe_id = str(summary["selected_primary_recipe_id"])

    monkeypatch.setattr(dashboard_app, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dashboard_app, "ML_PIPELINE_2_ARTIFACT_MODEL_CATALOG_DIR", tmp_path / "ml_pipeline_2" / "artifacts" / "published_models")
    monkeypatch.setattr(dashboard_app, "ARTIFACT_MODEL_CATALOG_DIR", tmp_path / "ml_pipeline" / "artifacts" / "models" / "by_features")

    entries = dashboard_app._build_artifact_discovery_entries()

    recovery_entries = [entry for entry in entries if entry.get("source") == "artifact_discovery_recovery"]
    assert recovery_entries

    entry = recovery_entries[0]
    assert entry["catalog_kind"] == "recovery"
    assert entry["profile_id"] == recipe_id
    assert entry["research_url"].startswith("/trading/research?")
    assert entry["evaluation_api_url"].startswith("/api/trading/research/evaluation?")
    assert entry["metrics"]["trades"] is not None
    assert any(path_row["label"] == "Recipe Summary" for path_row in entry["path_rows"])
    assert recipe_id in entry["title"]
