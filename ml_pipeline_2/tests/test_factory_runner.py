from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ml_pipeline_2.factory.monitor import LaneOutcome
from ml_pipeline_2.factory.runner import WorkflowRunner, resolve_workflow_root
from ml_pipeline_2.factory.spec import load_workflow_spec
from ml_pipeline_2.tests.helpers import build_staged_parquet_root, build_staged_smoke_manifest


def _build_spec(tmp_path: Path, *, stop_on_first_publishable: bool = False, depends_on_lane2: bool = True):
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    workflow_path = tmp_path / "factory.json"
    workflow_path.write_text(
        json.dumps(
            {
                "workflow_id": "wf1",
                "inputs": {"parquet_root": str(parquet_root), "support_dataset": "snapshots_ml_flat"},
                "lanes": [
                    {
                        "lane_id": "lane1",
                        "lane_kind": "staged_manifest",
                        "config_path": str(manifest_path),
                        "resource": {"cores": 2, "memory_gb": 2},
                    },
                    {
                        "lane_id": "lane2",
                        "lane_kind": "staged_manifest",
                        "config_path": str(manifest_path),
                        "depends_on": (["lane1"] if depends_on_lane2 else []),
                        "resource": {"cores": 2, "memory_gb": 2},
                    },
                ],
                "execution": {"poll_interval_seconds": 0, "infra_max_attempts": 2},
                "resource_budget": {"total_cores": 4, "total_memory_gb": 16},
                "selection": {"ranking_strategy": "publishable_economics_v1", "stop_on_first_publishable": stop_on_first_publishable},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return load_workflow_spec(workflow_path)


def _launch_result(lane_id: str, run_dir: Path, pid: int) -> SimpleNamespace:
    lane_root = Path(run_dir).resolve()
    return SimpleNamespace(
        pid=pid,
        lane_root=lane_root,
        runner_output_root=lane_root / "runner_output",
        log_path=lane_root / "factory_lane.log",
        command=("python", lane_id),
    )


def test_factory_runner_happy_path(monkeypatch, tmp_path: Path) -> None:
    spec = _build_spec(tmp_path)
    workflow_root = resolve_workflow_root(spec, tmp_path / "out")
    launched: list[str] = []

    def fake_launch(self, lane, *, lane_root):
        launched.append(lane.lane_id)
        return _launch_result(lane.lane_id, lane_root, 100 + len(launched))

    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.launch", fake_launch)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.is_alive", lambda self, pid: False)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.exit_code", lambda self, pid: 0)

    def fake_classify(lane, run_dir, *, exit_code):
        metrics = {"profit_factor": 1.6 if lane.lane_id == "lane1" else 1.4, "net_return_sum": 0.2 if lane.lane_id == "lane1" else 0.1, "stage2_roc_auc": 0.60}
        return LaneOutcome.PUBLISHABLE, metrics, Path(run_dir) / "summary.json", None

    monkeypatch.setattr("ml_pipeline_2.factory.runner.classify_lane_result", fake_classify)

    payload = WorkflowRunner(spec, workflow_root).run()

    assert payload["status"] == "publishable_found"
    assert payload["winner_lane_id"] == "lane1"
    assert launched == ["lane1", "lane2"]


def test_factory_runner_stops_on_first_publishable(monkeypatch, tmp_path: Path) -> None:
    spec = _build_spec(tmp_path, stop_on_first_publishable=True)
    workflow_root = resolve_workflow_root(spec, tmp_path / "out")
    launched: list[str] = []

    monkeypatch.setattr(
        "ml_pipeline_2.factory.runner.LaneLauncher.launch",
        lambda self, lane, *, lane_root: launched.append(lane.lane_id) or _launch_result(lane.lane_id, lane_root, 123),
    )
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.is_alive", lambda self, pid: False)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.exit_code", lambda self, pid: 0)
    monkeypatch.setattr(
        "ml_pipeline_2.factory.runner.classify_lane_result",
        lambda lane, run_dir, *, exit_code: (LaneOutcome.PUBLISHABLE, {"profit_factor": 1.7, "net_return_sum": 0.2, "stage2_roc_auc": 0.6}, Path(run_dir) / "summary.json", None),
    )

    payload = WorkflowRunner(spec, workflow_root).run()

    assert payload["winner_lane_id"] == "lane1"
    assert launched == ["lane1"]


def test_factory_runner_retries_infra_failure(monkeypatch, tmp_path: Path) -> None:
    spec = _build_spec(tmp_path)
    workflow_root = resolve_workflow_root(spec, tmp_path / "out")
    attempts = {"lane1": 0, "lane2": 0}

    monkeypatch.setattr(
        "ml_pipeline_2.factory.runner.LaneLauncher.launch",
        lambda self, lane, *, lane_root: attempts.__setitem__(lane.lane_id, attempts[lane.lane_id] + 1)
        or _launch_result(lane.lane_id, lane_root, 200 + attempts[lane.lane_id]),
    )
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.is_alive", lambda self, pid: False)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.exit_code", lambda self, pid: 0)

    def fake_classify(lane, run_dir, *, exit_code):
        if lane.lane_id == "lane1" and attempts["lane1"] == 1:
            return LaneOutcome.INFRA_FAILED, None, None, "oom"
        return LaneOutcome.PUBLISHABLE, {"profit_factor": 1.4, "net_return_sum": 0.1, "stage2_roc_auc": 0.6}, Path(run_dir) / "summary.json", None

    monkeypatch.setattr("ml_pipeline_2.factory.runner.classify_lane_result", fake_classify)

    payload = WorkflowRunner(spec, workflow_root).run()

    assert payload["status"] == "publishable_found"
    assert attempts["lane1"] == 2


def test_factory_runner_prunes_failed_dependency(monkeypatch, tmp_path: Path) -> None:
    spec = _build_spec(tmp_path)
    workflow_root = resolve_workflow_root(spec, tmp_path / "out")
    launched: list[str] = []

    monkeypatch.setattr(
        "ml_pipeline_2.factory.runner.LaneLauncher.launch",
        lambda self, lane, *, lane_root: launched.append(lane.lane_id) or _launch_result(lane.lane_id, lane_root, 123),
    )
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.is_alive", lambda self, pid: False)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.exit_code", lambda self, pid: 0)
    monkeypatch.setattr(
        "ml_pipeline_2.factory.runner.classify_lane_result",
        lambda lane, run_dir, *, exit_code: (LaneOutcome.GATE_FAILED, None, Path(run_dir) / "summary.json", "gate_failed"),
    )

    payload = WorkflowRunner(spec, workflow_root).run()

    assert payload["status"] == "no_publishable_candidate"
    assert launched == ["lane1"]
    assert any(item["lane_id"] == "lane2" and item["status"] == "gate_failed" for item in payload["failed_lanes"])


def test_factory_runner_resumes_from_partial_state(monkeypatch, tmp_path: Path) -> None:
    spec = _build_spec(tmp_path)
    workflow_root = resolve_workflow_root(spec, tmp_path / "out")
    workflow_root.mkdir(parents=True, exist_ok=True)
    (workflow_root / "workflow_state.json").write_text(
        json.dumps(
            {
                "workflow_id": spec.workflow_id,
                "workflow_root": str(workflow_root),
                "started_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "completed_at": None,
                "lanes": {
                    "lane1": {
                        "lane_id": "lane1",
                        "status": "publishable",
                        "attempt": 1,
                        "pid": None,
                        "run_dir": str(workflow_root / "lanes" / "01_lane1"),
                        "summary_path": str(workflow_root / "lanes" / "01_lane1" / "summary.json"),
                        "started_at": "2026-01-01T00:00:00Z",
                        "completed_at": "2026-01-01T00:10:00Z",
                        "metrics": {"profit_factor": 1.5, "net_return_sum": 0.15, "stage2_roc_auc": 0.6},
                        "last_error": None
                    },
                    "lane2": {
                        "lane_id": "lane2",
                        "status": "pending",
                        "attempt": 1,
                        "pid": None,
                        "run_dir": None,
                        "summary_path": None,
                        "started_at": None,
                        "completed_at": None,
                        "metrics": None,
                        "last_error": None
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    launched: list[str] = []
    monkeypatch.setattr(
        "ml_pipeline_2.factory.runner.LaneLauncher.launch",
        lambda self, lane, *, lane_root: launched.append(lane.lane_id) or _launch_result(lane.lane_id, lane_root, 456),
    )
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.is_alive", lambda self, pid: False)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.exit_code", lambda self, pid: 0)
    monkeypatch.setattr(
        "ml_pipeline_2.factory.runner.classify_lane_result",
        lambda lane, run_dir, *, exit_code: (LaneOutcome.HELD, {"profit_factor": 1.1, "net_return_sum": 0.01, "stage2_roc_auc": 0.55}, Path(run_dir) / "summary.json", "not_publishable"),
    )

    payload = WorkflowRunner(spec, workflow_root).run()

    assert launched == ["lane2"]
    assert payload["winner_lane_id"] == "lane1"


def test_factory_runner_cancels_running_lanes_after_publishable(monkeypatch, tmp_path: Path) -> None:
    spec = _build_spec(tmp_path, stop_on_first_publishable=True, depends_on_lane2=False)
    workflow_root = resolve_workflow_root(spec, tmp_path / "out")
    launched: list[str] = []
    terminated: list[int] = []
    alive_by_pid = {101: False, 102: True}

    def fake_launch(self, lane, *, lane_root):
        pid = 101 if lane.lane_id == "lane1" else 102
        launched.append(lane.lane_id)
        return _launch_result(lane.lane_id, lane_root, pid)

    def fake_is_alive(self, pid):
        return alive_by_pid.get(int(pid), False)

    def fake_terminate(self, pid):
        terminated.append(int(pid))
        alive_by_pid[int(pid)] = False

    def fake_classify(lane, run_dir, *, exit_code):
        if lane.lane_id == "lane1":
            return LaneOutcome.PUBLISHABLE, {"profit_factor": 1.8, "net_return_sum": 0.3, "stage2_roc_auc": 0.63}, Path(run_dir) / "summary.json", None
        raise AssertionError("lane2 should be canceled before classification")

    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.launch", fake_launch)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.is_alive", fake_is_alive)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.exit_code", lambda self, pid: 0)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.terminate", fake_terminate)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.classify_lane_result", fake_classify)

    payload = WorkflowRunner(spec, workflow_root).run()

    assert payload["winner_lane_id"] == "lane1"
    assert launched == ["lane1", "lane2"]
    assert terminated == [102]
    assert payload["canceled_lanes"] == ["lane2"]
    assert any(item["lane_id"] == "lane2" and item["status"] == "canceled" for item in payload["lane_summary"])


def test_factory_runner_reaps_orphaned_running_lane(monkeypatch, tmp_path: Path) -> None:
    spec = _build_spec(tmp_path)
    workflow_root = resolve_workflow_root(spec, tmp_path / "out")
    workflow_root.mkdir(parents=True, exist_ok=True)
    (workflow_root / "workflow_state.json").write_text(
        json.dumps(
            {
                "workflow_id": spec.workflow_id,
                "workflow_root": str(workflow_root),
                "started_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "completed_at": None,
                "lanes": {
                    "lane1": {
                        "lane_id": "lane1",
                        "status": "running",
                        "attempt": 1,
                        "pid": 999,
                        "run_dir": str(workflow_root / "lanes" / "01_lane1" / "runner_output"),
                        "summary_path": None,
                        "started_at": "2026-01-01T00:00:00Z",
                        "completed_at": None,
                        "metrics": None,
                        "last_error": None
                    },
                    "lane2": {
                        "lane_id": "lane2",
                        "status": "pending",
                        "attempt": 1,
                        "pid": None,
                        "run_dir": None,
                        "summary_path": None,
                        "started_at": None,
                        "completed_at": None,
                        "metrics": None,
                        "last_error": None
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    launched: list[str] = []

    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.is_alive", lambda self, pid: False)
    monkeypatch.setattr("ml_pipeline_2.factory.runner.LaneLauncher.exit_code", lambda self, pid: 0)
    monkeypatch.setattr(
        "ml_pipeline_2.factory.runner.classify_lane_result",
        lambda lane, run_dir, *, exit_code: (LaneOutcome.INFRA_FAILED, None, None, "orphaned"),
    )
    monkeypatch.setattr(
        "ml_pipeline_2.factory.runner.LaneLauncher.launch",
        lambda self, lane, *, lane_root: launched.append(lane.lane_id) or _launch_result(lane.lane_id, lane_root, 700 + len(launched)),
    )

    payload = WorkflowRunner(spec, workflow_root).run()

    assert launched[0] == "lane1"
    assert any(item["lane_id"] == "lane1" and item["attempts"] == 2 for item in payload["lane_summary"])
