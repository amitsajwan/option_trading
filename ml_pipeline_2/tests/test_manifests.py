from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml_pipeline_2.contracts.manifests import ManifestValidationError, load_and_resolve_manifest, resolve_manifest
from ml_pipeline_2.tests.helpers import build_synthetic_feature_frames


TUNED_TREE_MODELS = [
    "xgb_shallow",
    "xgb_balanced",
    "xgb_regularized",
    "xgb_deep_v1",
    "xgb_deep_slow_v1",
    "lgbm_fast",
    "lgbm_dart",
    "lgbm_large_v1",
    "lgbm_large_dart_v1",
]


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


def test_manifest_rejects_recovery_primary_model_outside_catalog(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/fo_expiry_aware_recovery.default.json").read_text(encoding="utf-8"))
    payload["catalog"]["models"] = ["xgb_shallow"]
    payload["scenario"]["primary_model"] = "xgb_deep_v1"
    with pytest.raises(ManifestValidationError):
        resolve_manifest(payload, manifest_path=tmp_path / "bad_recovery.json", validate_paths=False)


def test_manifest_validates_optional_runtime_model_n_jobs(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/fo_expiry_aware_recovery.default.json").read_text(encoding="utf-8"))
    payload["training"]["runtime"] = {"model_n_jobs": 4}
    resolved = resolve_manifest(payload, manifest_path=tmp_path / "recovery_runtime.json", validate_paths=False)
    assert resolved["training"]["runtime"]["model_n_jobs"] == 4

    payload["training"]["runtime"] = {"model_n_jobs": 0}
    with pytest.raises(ManifestValidationError):
        resolve_manifest(payload, manifest_path=tmp_path / "bad_runtime.json", validate_paths=False)


def test_manifest_validates_recovery_candidate_filter_block(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/fo_expiry_aware_recovery.fast_path_4y.json").read_text(encoding="utf-8"))
    resolved = resolve_manifest(payload, manifest_path=tmp_path / "recovery_fast_path.json", validate_paths=False)
    assert resolved["scenario"]["candidate_filter"]["require_event_sampled"] is True

    payload["scenario"]["candidate_filter"]["require_event_sampled"] = "yes"
    with pytest.raises(ManifestValidationError):
        resolve_manifest(payload, manifest_path=tmp_path / "bad_candidate_filter.json", validate_paths=False)


@pytest.mark.parametrize(
    "config_name,expected_windows",
    [
        (
            "fo_expiry_aware_recovery.tuning_1m_e2e.json",
            {
                "full_model": {"start": "2024-07-01", "end": "2024-07-31"},
                "final_holdout": {"start": "2024-08-01", "end": "2024-08-30"},
            },
        ),
        (
            "fo_expiry_aware_recovery.tuning_5m.json",
            {
                "full_model": {"start": "2024-03-01", "end": "2024-07-31"},
                "final_holdout": {"start": "2024-08-01", "end": "2024-08-31"},
            },
        ),
        (
            "fo_expiry_aware_recovery.tuning_4y.json",
            {
                "full_model": {"start": "2020-08-03", "end": "2024-07-31"},
                "final_holdout": {"start": "2024-08-01", "end": "2024-10-31"},
            },
        ),
        (
            "fo_expiry_aware_recovery.shortlist_4y.json",
            {
                "full_model": {"start": "2020-08-03", "end": "2024-07-31"},
                "final_holdout": {"start": "2024-08-01", "end": "2024-10-31"},
            },
        ),
        (
            "fo_expiry_aware_recovery.fast_path_4y.json",
            {
                "full_model": {"start": "2020-08-03", "end": "2024-07-31"},
                "final_holdout": {"start": "2024-08-01", "end": "2024-10-31"},
            },
        ),
    ],
)
def test_tuning_recovery_manifests_validate_with_new_model_catalog(
    tmp_path: Path,
    config_name: str,
    expected_windows: dict[str, dict[str, str]],
) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = Path("ml_pipeline_2/configs/research") / config_name
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["inputs"]["model_window_features_path"] = str(model_window_path)
    payload["inputs"]["holdout_features_path"] = str(holdout_path)
    payload["inputs"]["base_path"] = str(tmp_path)

    resolved = resolve_manifest(payload, manifest_path=tmp_path / config_name, validate_paths=True)

    if config_name == "fo_expiry_aware_recovery.shortlist_4y.json":
        assert resolved["catalog"]["models"] == ["xgb_balanced", "xgb_regularized", "xgb_shallow"]
        assert resolved["scenario"]["primary_model"] == "xgb_balanced"
        assert resolved["training"]["runtime"]["model_n_jobs"] == 4
    elif config_name == "fo_expiry_aware_recovery.fast_path_4y.json":
        assert resolved["catalog"]["models"] == ["xgb_shallow", "xgb_regularized"]
        assert resolved["scenario"]["primary_model"] == "xgb_shallow"
        assert resolved["scenario"]["candidate_filter"] == {
            "require_event_sampled": True,
            "exclude_expiry_day": True,
            "exclude_regime_atr_high": True,
            "require_tradeable_context": True,
            "allow_near_expiry_context": True,
        }
    else:
        assert resolved["catalog"]["models"] == TUNED_TREE_MODELS
        assert resolved["scenario"]["primary_model"] == "xgb_shallow"
    assert resolved["windows"] == expected_windows
