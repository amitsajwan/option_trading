from __future__ import annotations

import json
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
    assert summary["summary_schema_version"] == 2
    assert summary["experiment_kind"] == "staged_dual_recipe_v1"
    assert summary["component_ids"]["stage1"]["view_id"] == "stage1_entry_view_v1"
    assert summary["component_ids"]["stage2"]["trainer_id"] == "binary_catalog_v1"
    assert summary["component_ids"]["stage3"]["policy_id"] == "recipe_top_margin_v1"
    assert "publish_assessment" in summary
    assert sorted(summary["stage_artifacts"]) == ["stage1", "stage2", "stage3"]
    assert summary["stage_artifacts"]["stage1"]["started_at_utc"]
    assert summary["stage_artifacts"]["stage1"]["completed_at_utc"]
    assert Path(summary["stage_artifacts"]["stage1"]["model_package_path"]).exists()
    assert Path(summary["stage_artifacts"]["stage2"]["model_package_path"]).exists()
    assert Path(summary["stage_artifacts"]["stage3"]["training_report_path"]).exists()
    assert sorted(summary["stage_artifacts"]["stage3"]["recipes"]) == ["L0", "L1", "L2", "L3"]


def test_staged_runner_applies_block_expiry_runtime_filtering_to_training_frames(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["runtime"]["block_expiry"] = True
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_block_expiry_run",
    )

    assert summary["status"] == "completed"
    assert summary["runtime_block_expiry"] is True
    assert summary["runtime_filtering"]["block_expiry"]["enabled"] is True

    support_meta = summary["runtime_filtering"]["block_expiry"]["support"]
    assert support_meta["rows_before"] > support_meta["rows_after"]
    assert support_meta["expiry_rows_dropped"] > 0

    for stage_name in ("stage1", "stage2", "stage3"):
        stage_meta = summary["runtime_filtering"]["block_expiry"]["stages"][stage_name]
        assert stage_meta["rows_before"] > stage_meta["rows_after"]
        assert stage_meta["expiry_rows_dropped"] > 0
        assert stage_meta["signal_column"] in {"ctx_is_expiry_day", "ctx_dte_days"}
