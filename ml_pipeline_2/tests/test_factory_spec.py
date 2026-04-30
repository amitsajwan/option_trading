from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml_pipeline_2.factory.spec import load_workflow_spec
from ml_pipeline_2.tests.helpers import build_staged_grid_manifest, build_staged_parquet_root, build_staged_smoke_manifest


def _write_workflow(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_factory_spec_loads_valid_grid_lane(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = build_staged_grid_manifest(tmp_path, base_manifest_path)
    workflow_path = _write_workflow(
        tmp_path / "factory.json",
        {
            "workflow_id": "wf1",
            "inputs": {"parquet_root": str(parquet_root), "support_dataset": "snapshots_ml_flat"},
            "lanes": [
                {
                    "lane_id": "grid1",
                    "lane_kind": "staged_grid",
                    "config_path": str(grid_manifest_path),
                    "resource": {"cores": 4, "memory_gb": 8},
                    "model_group": "banknifty_futures/h15_tp_auto",
                    "profile_id": "openfe_v9_dual",
                }
            ],
            "execution": {"poll_interval_seconds": 0, "infra_max_attempts": 2},
            "resource_budget": {"total_cores": 8, "total_memory_gb": 32},
            "selection": {"ranking_strategy": "publishable_economics_v1", "stop_on_first_publishable": False},
        },
    )

    spec = load_workflow_spec(workflow_path)

    assert spec.workflow_id == "wf1"
    assert spec.lanes[0].lane_kind == "staged_grid"
    assert spec.lanes[0].summary_filename == "grid_summary.json"


def test_factory_spec_rejects_invalid_lane_kind(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    workflow_path = _write_workflow(
        tmp_path / "factory.json",
        {
            "workflow_id": "wf1",
            "inputs": {"parquet_root": str(parquet_root), "support_dataset": "snapshots_ml_flat"},
            "lanes": [{"lane_id": "bad", "lane_kind": "oops", "config_path": "x.json", "resource": {"cores": 1, "memory_gb": 1}}],
            "resource_budget": {"total_cores": 8, "total_memory_gb": 32},
            "selection": {"ranking_strategy": "publishable_economics_v1", "stop_on_first_publishable": False},
        },
    )
    with pytest.raises(ValueError, match="lane_kind"):
        load_workflow_spec(workflow_path)


def test_factory_spec_rejects_missing_dataset_root(tmp_path: Path) -> None:
    workflow_path = _write_workflow(
        tmp_path / "factory.json",
        {
            "workflow_id": "wf1",
            "inputs": {"parquet_root": str(tmp_path / "missing"), "support_dataset": "snapshots_ml_flat"},
            "lanes": [],
            "resource_budget": {"total_cores": 8, "total_memory_gb": 32},
            "selection": {"ranking_strategy": "publishable_economics_v1", "stop_on_first_publishable": False},
        },
    )
    with pytest.raises(ValueError, match="dataset root"):
        load_workflow_spec(workflow_path)


def test_factory_spec_rejects_cycle(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    workflow_path = _write_workflow(
        tmp_path / "factory.json",
        {
            "workflow_id": "wf1",
            "inputs": {"parquet_root": str(parquet_root), "support_dataset": "snapshots_ml_flat"},
            "lanes": [
                {
                    "lane_id": "a",
                    "lane_kind": "staged_manifest",
                    "config_path": str(base_manifest_path),
                    "depends_on": ["b"],
                    "resource": {"cores": 1, "memory_gb": 1},
                },
                {
                    "lane_id": "b",
                    "lane_kind": "staged_manifest",
                    "config_path": str(base_manifest_path),
                    "depends_on": ["a"],
                    "resource": {"cores": 1, "memory_gb": 1},
                },
            ],
            "resource_budget": {"total_cores": 8, "total_memory_gb": 32},
            "selection": {"ranking_strategy": "publishable_economics_v1", "stop_on_first_publishable": False},
        },
    )
    with pytest.raises(ValueError, match="cycle"):
        load_workflow_spec(workflow_path)


def test_factory_spec_rejects_lane_config_that_fails_runner_manifest_resolution(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["catalog"]["feature_sets_by_stage"]["stage2"] = ["definitely_not_a_real_feature_set"]
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    workflow_path = _write_workflow(
        tmp_path / "factory.json",
        {
            "workflow_id": "wf1",
            "inputs": {"parquet_root": str(parquet_root), "support_dataset": "snapshots_ml_flat"},
            "lanes": [
                {
                    "lane_id": "bad_manifest",
                    "lane_kind": "staged_manifest",
                    "config_path": str(manifest_path),
                    "resource": {"cores": 1, "memory_gb": 1},
                }
            ],
            "resource_budget": {"total_cores": 8, "total_memory_gb": 32},
            "selection": {"ranking_strategy": "publishable_economics_v1", "stop_on_first_publishable": False},
        },
    )

    with pytest.raises(ValueError, match="failed manifest resolution"):
        load_workflow_spec(workflow_path)
