from __future__ import annotations

import json
import shutil
from pathlib import Path

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.run_direction_from_move_quick import _build_parser as build_direction_parser
from ml_pipeline_2.run_direction_from_move_quick import _resolve_config as resolve_direction_config
from ml_pipeline_2.run_move_detector_quick import _build_parser as build_move_parser
from ml_pipeline_2.run_move_detector_quick import _resolve_config as resolve_move_config
from ml_pipeline_2.run_recovery_matrix import _build_parser as build_matrix_parser
from ml_pipeline_2.run_recovery_matrix import _resolve_args as resolve_matrix_args
from ml_pipeline_2.tests.helpers import build_synthetic_feature_frames


def _copy_default_json(source: Path, dest: Path) -> Path:
    payload = json.loads(source.read_text(encoding="utf-8"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return dest


def _seed_external_cache(workspace: Path) -> tuple[Path, Path]:
    model_window_src, holdout_src = build_synthetic_feature_frames(workspace / "source_data")
    frozen_root = workspace / ".data" / "ml_pipeline" / "frozen"
    frozen_root.mkdir(parents=True, exist_ok=True)
    model_window_dest = frozen_root / "model_window_features.parquet"
    holdout_dest = frozen_root / "holdout_features.parquet"
    shutil.copy2(model_window_src, model_window_dest)
    shutil.copy2(holdout_src, holdout_dest)
    return model_window_dest, holdout_dest


def test_default_phase2_manifest_resolves_against_manifest_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest_path = _copy_default_json(
        Path("ml_pipeline_2/configs/research/phase2_label_sweep.default.json"),
        workspace / "ml_pipeline_2" / "configs" / "research" / "phase2_label_sweep.default.json",
    )
    model_window_path, holdout_path = _seed_external_cache(workspace)

    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    assert resolved["inputs"]["model_window_features_path"] == model_window_path
    assert resolved["inputs"]["holdout_features_path"] == holdout_path
    assert resolved["inputs"]["base_path"] == workspace / ".data" / "ml_pipeline"
    assert resolved["outputs"]["artifacts_root"] == workspace / "ml_pipeline_2" / "artifacts" / "research"


def test_move_detector_config_resolves_relative_paths_from_config_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    model_window_path, holdout_path = _seed_external_cache(workspace)
    config_path = _copy_default_json(
        Path("ml_pipeline_2/configs/research/move_detector_quick.default.json"),
        workspace / "ml_pipeline_2" / "configs" / "research" / "move_detector_quick.default.json",
    )

    args = build_move_parser().parse_args(["--config", str(config_path)])
    resolved = resolve_move_config(args)

    assert Path(resolved["inputs"]["model_window_features"]) == model_window_path
    assert Path(resolved["inputs"]["holdout_features"]) == holdout_path
    assert Path(resolved["outputs"]["out_root"]) == workspace / "ml_pipeline_2" / "artifacts" / "research"


def test_direction_config_resolves_stage1_run_dir_from_config_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    stage1_run_dir = workspace / "ml_pipeline_2" / "artifacts" / "research" / "move_detector_quick_20260312_010101"
    stage1_run_dir.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "ml_pipeline_2" / "configs" / "research" / "direction_from_move_quick.default.json"
    payload = {
        "inputs": {
            "stage1_run_dir": "../../artifacts/research/move_detector_quick_20260312_010101"
        },
        "training": {
            "feature_profile": "all",
            "feature_sets": ["fo_expiry_aware_v2"],
            "models": ["xgb_shallow"],
            "max_experiments": 1,
            "objective": "brier",
            "cv": {"train_days": 60, "valid_days": 15, "test_days": 15, "step_days": 15},
        },
        "gating": {
            "move_threshold": 0.6,
            "direction_threshold_grid": [0.55, 0.6, 0.65],
            "cost_per_trade": 0.0006,
        },
        "outputs": {
            "out_root": "../../artifacts/research",
            "run_name": "direction_from_move_quick",
            "run_dir": "",
            "resume": False,
        },
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    args = build_direction_parser().parse_args(["--config", str(config_path)])
    resolved = resolve_direction_config(args)

    assert Path(resolved["inputs"]["stage1_run_dir"]) == stage1_run_dir
    assert Path(resolved["outputs"]["out_root"]) == workspace / "ml_pipeline_2" / "artifacts" / "research"


def test_recovery_matrix_config_resolves_paths_from_config_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config_path = _copy_default_json(
        Path("ml_pipeline_2/configs/research/recovery_matrix.default.json"),
        workspace / "ml_pipeline_2" / "configs" / "research" / "recovery_matrix.default.json",
    )
    base_manifest_path = workspace / "ml_pipeline_2" / "configs" / "research" / "fo_expiry_aware_recovery.default.json"
    base_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    base_manifest_path.write_text("{}", encoding="utf-8")

    args = build_matrix_parser().parse_args(["--config", str(config_path)])
    resolved = resolve_matrix_args(args)

    assert Path(resolved["base_manifest_path"]) == base_manifest_path
    assert Path(resolved["existing_matrix_root"]) == workspace / "ml_pipeline_2" / "artifacts" / "research_matrices"
    assert Path(resolved["matrix_root"]).parent == workspace / "ml_pipeline_2" / "artifacts" / "research_matrices"
    assert Path(resolved["job_root"]) == workspace / "ml_pipeline_2" / "artifacts" / "background_jobs"


def test_active_ubuntu_docs_and_configs_do_not_contain_windows_absolute_paths() -> None:
    targets = [
        Path("ml_pipeline_2/README.md"),
        Path("ml_pipeline_2/docs/ubuntu_gcp_runbook.md"),
        Path("ml_pipeline_2/pyproject.toml"),
        *sorted(Path("ml_pipeline_2/configs/research").glob("*.json")),
    ]
    offenders = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        if "C:\\" in text or "C:/" in text:
            offenders.append(str(path))
    assert offenders == []
