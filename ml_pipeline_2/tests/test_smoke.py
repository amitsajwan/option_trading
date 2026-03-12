from __future__ import annotations

from pathlib import Path

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_research
from ml_pipeline_2.tests.helpers import build_phase2_smoke_manifest, build_recovery_smoke_manifest, build_synthetic_feature_frames


def test_phase2_smoke_runs_end_to_end(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_phase2_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    output_root = Path(summary["output_root"])
    assert (output_root / "resolved_config.json").exists()
    assert (output_root / "manifest_hash.txt").exists()
    assert (output_root / "state.jsonl").exists()
    assert (output_root / "phase2_summary.json").exists()
    assert (output_root / "phase2_binary_baseline.json").exists()
    assert (output_root / "recipes" / "L1" / "selection_summary.json").exists()
    assert (output_root / "model_stress" / "L1" / "model_stress_summary.json").exists()


def test_recovery_smoke_runs_end_to_end(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    output_root = Path(summary["output_root"])
    assert (output_root / "resolved_config.json").exists()
    assert (output_root / "manifest_hash.txt").exists()
    assert (output_root / "state.jsonl").exists()
    assert (output_root / "summary.json").exists()
    assert (output_root / "primary_recipes" / "TB_BASE_L1" / "summary.json").exists()
    assert (output_root / "meta_gate" / "summary.json").exists()
