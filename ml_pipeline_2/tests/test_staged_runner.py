from __future__ import annotations

from pathlib import Path

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_manifest
from ml_pipeline_2.tests.helpers import build_staged_parquet_root, build_staged_smoke_manifest


def test_staged_runner_builds_summary_and_stage_artifacts(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_smoke_test_run",
    )

    assert summary["status"] == "completed"
    assert summary["experiment_kind"] == "staged_dual_recipe_v1"
    assert summary["component_ids"]["stage1"]["view_id"] == "stage1_entry_view_v1"
    assert summary["component_ids"]["stage2"]["trainer_id"] == "binary_catalog_v1"
    assert summary["component_ids"]["stage3"]["policy_id"] == "recipe_top_margin_v1"
    assert "publish_assessment" in summary
    assert sorted(summary["stage_artifacts"]) == ["stage1", "stage2", "stage3"]
    assert Path(summary["stage_artifacts"]["stage1"]["model_package_path"]).exists()
    assert Path(summary["stage_artifacts"]["stage2"]["model_package_path"]).exists()
    assert Path(summary["stage_artifacts"]["stage3"]["training_report_path"]).exists()
    assert sorted(summary["stage_artifacts"]["stage3"]["recipes"]) == ["L0", "L1", "L2", "L3"]
