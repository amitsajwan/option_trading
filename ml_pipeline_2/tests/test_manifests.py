from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml_pipeline_2.contracts.manifests import ManifestValidationError, resolve_manifest


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


def test_staged_manifest_accepts_random_state_zero(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    payload["training"]["random_state"] = 0
    resolved = resolve_manifest(payload, manifest_path=tmp_path / "staged_random_state_zero.json", validate_paths=False)
    assert resolved["training"]["random_state"] == 0


def test_staged_manifest_accepts_runtime_block_expiry_bool(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    payload["runtime"]["block_expiry"] = True
    resolved = resolve_manifest(payload, manifest_path=tmp_path / "staged_block_expiry_true.json", validate_paths=False)
    assert resolved["runtime"]["block_expiry"] is True


def test_staged_manifest_rejects_runtime_block_expiry_non_bool(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    payload["runtime"]["block_expiry"] = "true"
    with pytest.raises(ManifestValidationError, match="runtime.block_expiry must be boolean"):
        resolve_manifest(payload, manifest_path=tmp_path / "staged_block_expiry_invalid.json", validate_paths=False)


def test_staged_manifest_rejects_profit_factor_floor_below_one(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    payload["hard_gates"]["combined"]["profit_factor_min"] = 0.99
    with pytest.raises(ManifestValidationError, match="hard_gates.combined.profit_factor_min must be >= 1.0"):
        resolve_manifest(payload, manifest_path=tmp_path / "staged_bad_profit_factor.json", validate_paths=False)


def test_staged_manifest_accepts_publish_smoke_allow_non_publishable_bool(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    payload["publish"]["smoke_allow_non_publishable"] = True
    resolved = resolve_manifest(payload, manifest_path=tmp_path / "staged_publish_smoke_bool_ok.json", validate_paths=False)
    assert resolved["publish"]["smoke_allow_non_publishable"] is True


def test_staged_manifest_rejects_publish_smoke_allow_non_publishable_non_bool(tmp_path: Path) -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    payload["publish"]["smoke_allow_non_publishable"] = "true"
    with pytest.raises(ManifestValidationError, match="publish.smoke_allow_non_publishable must be boolean"):
        resolve_manifest(payload, manifest_path=tmp_path / "staged_publish_smoke_bool_bad.json", validate_paths=False)


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


def test_grid_manifest_validates_with_supported_override_contract(tmp_path: Path) -> None:
    base_payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    base_path = tmp_path / "base_manifest.json"
    base_path.write_text(json.dumps(base_payload, indent=2), encoding="utf-8")

    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_training_grid_v1",
        "inputs": {
            "base_manifest_path": str(base_path),
        },
        "outputs": {
            "artifacts_root": str(tmp_path / "grid_artifacts"),
            "run_name": "staged_grid_prod_v1",
        },
        "selection": {
            "stage2_hpo_escalation": {
                "roc_auc_min": 0.54,
                "brier_max": 0.225,
            }
        },
        "grid": {
            "research_only": True,
            "max_parallel_runs": 2,
            "runs": [
                {
                    "run_id": "baseline",
                    "model_group_suffix": "_baseline",
                    "overrides": {
                        "outputs": {"run_name": "staged_grid_baseline"},
                    },
                },
                {
                    "run_id": "edge_0010",
                    "model_group_suffix": "_edge_0010",
                    "overrides": {
                        "training": {
                            "stage2_label_filter": {
                                "enabled": True,
                                "min_directional_edge_after_cost": 0.001,
                            }
                        }
                    },
                },
                {
                    "run_id": "best_edge_block_expiry",
                    "model_group_suffix": "_best_edge_block_expiry",
                    "inherit_best_from": ["edge_0010"],
                    "overrides": {
                        "runtime": {"block_expiry": True},
                        "catalog": {
                            "feature_sets_by_stage": {
                                "stage2": ["fo_expiry_aware_v3", "fo_no_time_context"],
                            }
                        },
                    },
                },
            ],
        },
    }

    resolved = resolve_manifest(payload, manifest_path=tmp_path / "staged_grid.json", validate_paths=False)

    assert resolved["experiment_kind"] == "staged_training_grid_v1"
    assert resolved["grid"]["max_parallel_runs"] == 2
    assert resolved["grid"]["runs"][1]["overrides"]["training"]["stage2_label_filter"]["min_directional_edge_after_cost"] == 0.001
    assert resolved["base_resolved_manifest"]["experiment_kind"] == "staged_dual_recipe_v1"


def test_grid_manifest_rejects_unsupported_override_paths(tmp_path: Path) -> None:
    base_payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    base_path = tmp_path / "base_manifest.json"
    base_path.write_text(json.dumps(base_payload, indent=2), encoding="utf-8")
    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_training_grid_v1",
        "inputs": {
            "base_manifest_path": str(base_path),
        },
        "outputs": {
            "artifacts_root": str(tmp_path / "grid_artifacts"),
            "run_name": "staged_grid_prod_v1",
        },
        "selection": {
            "stage2_hpo_escalation": {
                "roc_auc_min": 0.54,
                "brier_max": 0.225,
            }
        },
        "grid": {
            "research_only": True,
            "max_parallel_runs": 2,
            "runs": [
                {
                    "run_id": "bad_override",
                    "overrides": {
                        "catalog": {"models_by_stage": {"stage1": ["logreg_balanced"]}},
                    },
                }
            ],
        },
    }

    with pytest.raises(ManifestValidationError, match="supports only feature_sets_by_stage"):
        resolve_manifest(payload, manifest_path=tmp_path / "staged_grid_invalid.json", validate_paths=False)


def test_grid_manifest_rejects_non_positive_parallelism(tmp_path: Path) -> None:
    base_payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    base_path = tmp_path / "base_manifest.json"
    base_path.write_text(json.dumps(base_payload, indent=2), encoding="utf-8")
    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_training_grid_v1",
        "inputs": {
            "base_manifest_path": str(base_path),
        },
        "outputs": {
            "artifacts_root": str(tmp_path / "grid_artifacts"),
            "run_name": "staged_grid_prod_v1",
        },
        "selection": {
            "stage2_hpo_escalation": {
                "roc_auc_min": 0.54,
                "brier_max": 0.225,
            }
        },
        "grid": {
            "research_only": True,
            "max_parallel_runs": 0,
            "runs": [
                {
                    "run_id": "baseline",
                    "overrides": {
                        "outputs": {"run_name": "staged_grid_baseline"},
                    },
                }
            ],
        },
    }

    with pytest.raises(ManifestValidationError, match="grid.max_parallel_runs must be an integer > 0"):
        resolve_manifest(payload, manifest_path=tmp_path / "staged_grid_bad_parallelism.json", validate_paths=False)

