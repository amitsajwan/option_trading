from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml_pipeline_2.factory.spec import load_workflow_spec
from ml_pipeline_2.factory.state import LaneStatus, WorkflowState
from ml_pipeline_2.tests.helpers import build_staged_parquet_root, build_staged_smoke_manifest


def _build_spec(tmp_path: Path):
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    workflow_path = tmp_path / "factory.json"
    workflow_path.write_text(
        json.dumps(
            {
                "workflow_id": "wf1",
                "inputs": {"parquet_root": str(parquet_root), "support_dataset": "snapshots_ml_flat"},
                "lanes": [{"lane_id": "m1", "lane_kind": "staged_manifest", "config_path": str(manifest_path), "resource": {"cores": 1, "memory_gb": 1}}],
                "execution": {"poll_interval_seconds": 0, "infra_max_attempts": 2},
                "resource_budget": {"total_cores": 8, "total_memory_gb": 32},
                "selection": {"ranking_strategy": "publishable_economics_v1", "stop_on_first_publishable": False},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return load_workflow_spec(workflow_path)


def test_factory_state_initializes_pending(tmp_path: Path) -> None:
    spec = _build_spec(tmp_path)
    state = WorkflowState.load(tmp_path / "workflow_root", spec)
    assert state.get("m1").status == LaneStatus.PENDING


def test_factory_state_ignores_stray_tmp_file_on_reload(tmp_path: Path) -> None:
    spec = _build_spec(tmp_path)
    workflow_root = tmp_path / "workflow_root"
    state = WorkflowState.load(workflow_root, spec)
    tmp_state = workflow_root / "workflow_state.json.tmp"
    tmp_state.write_text("{\"corrupt\":", encoding="utf-8")

    reloaded = WorkflowState.load(workflow_root, spec)

    assert reloaded.get("m1").status == LaneStatus.PENDING


def test_factory_state_rejects_illegal_transition(tmp_path: Path) -> None:
    spec = _build_spec(tmp_path)
    state = WorkflowState.load(tmp_path / "workflow_root", spec)
    state.transition("m1", LaneStatus.RUNNING, pid=123, run_dir=str(tmp_path))
    state.mark_completed("m1", status=LaneStatus.PUBLISHABLE, metrics={"profit_factor": 1.2}, summary_path=None, error=None)
    with pytest.raises(ValueError, match="illegal lane transition"):
        state.transition("m1", LaneStatus.RUNNING)
