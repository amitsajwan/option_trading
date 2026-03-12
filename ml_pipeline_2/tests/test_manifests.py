from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml_pipeline_2.contracts.manifests import ManifestValidationError, load_and_resolve_manifest, resolve_manifest
from ml_pipeline_2.tests.helpers import build_synthetic_feature_frames


def test_manifest_rejects_unknown_model(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/phase2_label_sweep.default.json").read_text(encoding="utf-8"))
    payload["catalog"]["models"] = ["does_not_exist"]
    with pytest.raises(ManifestValidationError):
        resolve_manifest(payload, manifest_path=tmp_path / "bad.json", validate_paths=False)


def test_manifest_rejects_invalid_windows(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/phase2_label_sweep.default.json").read_text(encoding="utf-8"))
    payload["windows"]["research_train"]["end"] = "2024-05-01"
    payload["windows"]["research_valid"]["start"] = "2024-05-01"
    with pytest.raises(ManifestValidationError):
        resolve_manifest(payload, manifest_path=tmp_path / "bad.json", validate_paths=False)


def test_manifest_rejects_empty_threshold_grid(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/phase2_label_sweep.default.json").read_text(encoding="utf-8"))
    payload["scenario"]["threshold_grid"] = []
    with pytest.raises(ManifestValidationError):
        resolve_manifest(payload, manifest_path=tmp_path / "bad.json", validate_paths=False)


def test_manifest_rejects_missing_input_paths(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    payload = json.loads(Path("ml_pipeline_2/configs/research/phase2_label_sweep.default.json").read_text(encoding="utf-8"))
    payload["inputs"]["model_window_features_path"] = str(tmp_path / "missing_model.parquet")
    payload["inputs"]["holdout_features_path"] = str(tmp_path / "missing_holdout.parquet")
    payload["inputs"]["base_path"] = str(tmp_path / "missing_base")
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with pytest.raises(ManifestValidationError):
        load_and_resolve_manifest(manifest_path, validate_paths=True)


def test_manifest_accepts_real_paths(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    payload = json.loads(Path("ml_pipeline_2/configs/research/phase2_label_sweep.default.json").read_text(encoding="utf-8"))
    payload["inputs"]["model_window_features_path"] = str(model_window_path)
    payload["inputs"]["holdout_features_path"] = str(holdout_path)
    payload["inputs"]["base_path"] = str(tmp_path)
    resolved = resolve_manifest(payload, manifest_path=tmp_path / "manifest.json", validate_paths=True)
    assert resolved["inputs"]["model_window_features_path"] == model_window_path

