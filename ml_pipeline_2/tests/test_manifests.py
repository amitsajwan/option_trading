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


def test_staged_manifest_requires_explicit_sections(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_dual_recipe_v1",
        "inputs": {},
        "outputs": {},
        "catalog": {},
        "windows": {},
        "training": {},
    }
    with pytest.raises(ManifestValidationError):
        resolve_manifest(payload, manifest_path=tmp_path / "staged_missing.json", validate_paths=False)


def test_staged_manifest_validates_with_explicit_contract(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_dual_recipe_v1",
        "inputs": {
            "parquet_root": str(tmp_path / "parquet"),
            "support_dataset": "snapshots_ml_flat",
        },
        "outputs": {
            "artifacts_root": str(tmp_path / "artifacts"),
            "run_name": "staged_smoke",
        },
        "catalog": {
            "models_by_stage": {
                "stage1": ["logreg_balanced"],
                "stage2": ["logreg_balanced"],
                "stage3": ["logreg_balanced"],
            },
            "feature_sets_by_stage": {
                "stage1": ["fo_full"],
                "stage2": ["fo_full"],
                "stage3": ["fo_full"],
            },
            "recipe_catalog_id": "fixed_l0_l3_v1",
        },
        "windows": {
            "research_train": {"start": "2024-01-01", "end": "2024-01-08"},
            "research_valid": {"start": "2024-01-09", "end": "2024-01-12"},
            "full_model": {"start": "2024-01-01", "end": "2024-01-12"},
            "final_holdout": {"start": "2024-01-13", "end": "2024-01-18"},
        },
        "views": {
            "stage1_view_id": "stage1_entry_view_v1",
            "stage2_view_id": "stage2_direction_view_v1",
            "stage3_view_id": "stage3_recipe_view_v1",
        },
        "labels": {
            "stage1_labeler_id": "entry_best_recipe_v1",
            "stage2_labeler_id": "direction_best_recipe_v1",
            "stage3_labeler_id": "recipe_best_positive_v1",
        },
        "training": {
            "stage1_trainer_id": "binary_catalog_v1",
            "stage2_trainer_id": "binary_catalog_v1",
            "stage3_trainer_id": "ovr_recipe_catalog_v1",
            "preprocess": {"max_missing_rate": 0.35, "clip_lower_q": 0.01, "clip_upper_q": 0.99},
            "cv_config": {"train_days": 4, "valid_days": 2, "test_days": 2, "step_days": 2, "purge_days": 0, "embargo_days": 0, "purge_mode": "days", "embargo_rows": 0, "event_end_col": None},
            "objectives_by_stage": {"stage1": "brier", "stage2": "brier", "stage3": "brier"},
            "random_state": 42,
            "runtime": {"model_n_jobs": 1},
            "cost_per_trade": 0.0006,
        },
        "policy": {
            "stage1_policy_id": "entry_threshold_v1",
            "stage2_policy_id": "direction_dual_threshold_v1",
            "stage3_policy_id": "recipe_top_margin_v1",
            "stage1": {"threshold_grid": [0.45, 0.50, 0.55]},
            "stage2": {"ce_threshold_grid": [0.55], "pe_threshold_grid": [0.55], "min_edge_grid": [0.05, 0.10]},
            "stage3": {"threshold_grid": [0.45, 0.50], "margin_grid": [0.02, 0.05]},
        },
        "runtime": {"prefilter_gate_ids": ["rollout_guard_v1", "feature_freshness_v1"]},
        "publish": {"publisher_id": "staged_bundle_v1"},
        "hard_gates": {
            "stage1": {"roc_auc_min": 0.55, "brier_max": 0.22, "roc_auc_drift_half_split_max_abs": 0.05},
            "stage2": {"roc_auc_min": 0.55, "brier_max": 0.22},
            "stage3": {"max_drawdown_slack": 0.01},
            "combined": {"profit_factor_min": 1.10, "max_drawdown_pct_max": 0.25, "trades_min": 1, "net_return_sum_min": 0.0, "side_share_min": 0.0, "side_share_max": 1.0, "block_rate_min": 0.0},
        },
    }
    resolved = resolve_manifest(payload, manifest_path=tmp_path / "staged_ok.json", validate_paths=False)
    assert resolved["experiment_kind"] == "staged_dual_recipe_v1"
    assert resolved["catalog"]["recipe_catalog_id"] == "fixed_l0_l3_v1"
    assert resolved["views"]["stage1_view_id"] == "stage1_entry_view_v1"


def test_staged_manifest_validate_paths_requires_stage_view_datasets(tmp_path: Path) -> None:
    parquet_root = tmp_path / "parquet"
    (parquet_root / "snapshots_ml_flat").mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_dual_recipe_v1",
        "inputs": {
            "parquet_root": str(parquet_root),
            "support_dataset": "snapshots_ml_flat",
        },
        "outputs": {
            "artifacts_root": str(tmp_path / "artifacts"),
            "run_name": "staged_smoke",
        },
        "catalog": {
            "models_by_stage": {"stage1": ["logreg_balanced"], "stage2": ["logreg_balanced"], "stage3": ["logreg_balanced"]},
            "feature_sets_by_stage": {"stage1": ["fo_full"], "stage2": ["fo_full"], "stage3": ["fo_full"]},
            "recipe_catalog_id": "fixed_l0_l3_v1",
        },
        "windows": {
            "research_train": {"start": "2024-01-01", "end": "2024-01-08"},
            "research_valid": {"start": "2024-01-09", "end": "2024-01-12"},
            "full_model": {"start": "2024-01-01", "end": "2024-01-12"},
            "final_holdout": {"start": "2024-01-13", "end": "2024-01-18"},
        },
        "views": {
            "stage1_view_id": "stage1_entry_view_v1",
            "stage2_view_id": "stage2_direction_view_v1",
            "stage3_view_id": "stage3_recipe_view_v1",
        },
        "labels": {
            "stage1_labeler_id": "entry_best_recipe_v1",
            "stage2_labeler_id": "direction_best_recipe_v1",
            "stage3_labeler_id": "recipe_best_positive_v1",
        },
        "training": {
            "stage1_trainer_id": "binary_catalog_v1",
            "stage2_trainer_id": "binary_catalog_v1",
            "stage3_trainer_id": "ovr_recipe_catalog_v1",
            "preprocess": {"max_missing_rate": 0.35, "clip_lower_q": 0.01, "clip_upper_q": 0.99},
            "cv_config": {"train_days": 4, "valid_days": 2, "test_days": 2, "step_days": 2, "purge_days": 0, "embargo_days": 0, "purge_mode": "days", "embargo_rows": 0, "event_end_col": None},
            "objectives_by_stage": {"stage1": "brier", "stage2": "brier", "stage3": "brier"},
            "random_state": 42,
            "runtime": {"model_n_jobs": 1},
            "cost_per_trade": 0.0006,
        },
        "policy": {
            "stage1_policy_id": "entry_threshold_v1",
            "stage2_policy_id": "direction_dual_threshold_v1",
            "stage3_policy_id": "recipe_top_margin_v1",
            "stage1": {"threshold_grid": [0.45]},
            "stage2": {"ce_threshold_grid": [0.55], "pe_threshold_grid": [0.55], "min_edge_grid": [0.05]},
            "stage3": {"threshold_grid": [0.45], "margin_grid": [0.02]},
        },
        "runtime": {"prefilter_gate_ids": ["rollout_guard_v1", "feature_freshness_v1"]},
        "publish": {"publisher_id": "staged_bundle_v1"},
        "hard_gates": {
            "stage1": {"roc_auc_min": 0.55, "brier_max": 0.22, "roc_auc_drift_half_split_max_abs": 0.05},
            "stage2": {"roc_auc_min": 0.55, "brier_max": 0.22},
            "stage3": {"max_drawdown_slack": 0.01},
            "combined": {"profit_factor_min": 1.10, "max_drawdown_pct_max": 0.25, "trades_min": 1, "net_return_sum_min": 0.0, "side_share_min": 0.0, "side_share_max": 1.0, "block_rate_min": 0.0},
        },
    }
    with pytest.raises(ManifestValidationError, match="views.stage1_view_id dataset not found under parquet_root"):
        resolve_manifest(payload, manifest_path=tmp_path / "staged_paths_bad.json", validate_paths=True)

    for dataset_name in ("stage1_entry_view", "stage2_direction_view", "stage3_recipe_view"):
        (parquet_root / dataset_name).mkdir(parents=True, exist_ok=True)
    resolved = resolve_manifest(payload, manifest_path=tmp_path / "staged_paths_ok.json", validate_paths=True)
    assert resolved["inputs"]["parquet_root"] == parquet_root


def test_staged_manifest_requires_explicit_hard_gate_fields(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_dual_recipe_v1",
        "inputs": {"parquet_root": str(tmp_path / "parquet"), "support_dataset": "snapshots_ml_flat"},
        "outputs": {"artifacts_root": str(tmp_path / "artifacts"), "run_name": "staged_smoke"},
        "catalog": {
            "models_by_stage": {"stage1": ["logreg_balanced"], "stage2": ["logreg_balanced"], "stage3": ["logreg_balanced"]},
            "feature_sets_by_stage": {"stage1": ["fo_full"], "stage2": ["fo_full"], "stage3": ["fo_full"]},
            "recipe_catalog_id": "fixed_l0_l3_v1",
        },
        "windows": {
            "research_train": {"start": "2024-01-01", "end": "2024-01-08"},
            "research_valid": {"start": "2024-01-09", "end": "2024-01-12"},
            "full_model": {"start": "2024-01-01", "end": "2024-01-12"},
            "final_holdout": {"start": "2024-01-13", "end": "2024-01-18"},
        },
        "views": {
            "stage1_view_id": "stage1_entry_view_v1",
            "stage2_view_id": "stage2_direction_view_v1",
            "stage3_view_id": "stage3_recipe_view_v1",
        },
        "labels": {
            "stage1_labeler_id": "entry_best_recipe_v1",
            "stage2_labeler_id": "direction_best_recipe_v1",
            "stage3_labeler_id": "recipe_best_positive_v1",
        },
        "training": {
            "stage1_trainer_id": "binary_catalog_v1",
            "stage2_trainer_id": "binary_catalog_v1",
            "stage3_trainer_id": "ovr_recipe_catalog_v1",
            "preprocess": {"max_missing_rate": 0.35, "clip_lower_q": 0.01, "clip_upper_q": 0.99},
            "cv_config": {"train_days": 4, "valid_days": 2, "test_days": 2, "step_days": 2, "purge_days": 0, "embargo_days": 0, "purge_mode": "days", "embargo_rows": 0, "event_end_col": None},
            "objectives_by_stage": {"stage1": "brier", "stage2": "brier", "stage3": "brier"},
            "random_state": 42,
            "runtime": {"model_n_jobs": 1},
            "cost_per_trade": 0.0006,
        },
        "policy": {
            "stage1_policy_id": "entry_threshold_v1",
            "stage2_policy_id": "direction_dual_threshold_v1",
            "stage3_policy_id": "recipe_top_margin_v1",
            "stage1": {"threshold_grid": [0.45]},
            "stage2": {"ce_threshold_grid": [0.55], "pe_threshold_grid": [0.55], "min_edge_grid": [0.05]},
            "stage3": {"threshold_grid": [0.45], "margin_grid": [0.02]},
        },
        "runtime": {"prefilter_gate_ids": ["rollout_guard_v1", "feature_freshness_v1"]},
        "publish": {"publisher_id": "staged_bundle_v1"},
        "hard_gates": {
            "stage1": {"roc_auc_min": 0.55, "brier_max": 0.22},
            "stage2": {"roc_auc_min": 0.55, "brier_max": 0.22},
            "stage3": {"max_drawdown_slack": 0.01},
            "combined": {"profit_factor_min": 1.10, "max_drawdown_pct_max": 0.25, "trades_min": 1, "net_return_sum_min": 0.0, "side_share_min": 0.0, "side_share_max": 1.0, "block_rate_min": 0.0},
        },
    }
    with pytest.raises(ManifestValidationError, match="hard_gates.stage1.roc_auc_drift_half_split_max_abs must be set"):
        resolve_manifest(payload, manifest_path=tmp_path / "staged_missing_gate.json", validate_paths=False)


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
