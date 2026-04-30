from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from ..experiment_control.state import utc_now
from .spec import WorkflowSpec


class LaneStatus(str, Enum):
    PENDING = "pending"
    WAITING_RESOURCE = "waiting_resource"
    RUNNING = "running"
    PUBLISHABLE = "publishable"
    HELD = "held"
    GATE_FAILED = "gate_failed"
    INFRA_FAILED = "infra_failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            LaneStatus.PUBLISHABLE,
            LaneStatus.HELD,
            LaneStatus.GATE_FAILED,
            LaneStatus.INFRA_FAILED,
            LaneStatus.CANCELED,
        }


@dataclass
class LaneState:
    lane_id: str
    status: LaneStatus
    attempt: int = 1
    pid: Optional[int] = None
    run_dir: Optional[str] = None
    summary_path: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "status": self.status.value,
            "attempt": int(self.attempt),
            "pid": self.pid,
            "run_dir": self.run_dir,
            "summary_path": self.summary_path,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "metrics": self.metrics,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "LaneState":
        return cls(
            lane_id=str(payload["lane_id"]),
            status=LaneStatus(str(payload["status"])),
            attempt=int(payload.get("attempt", 1)),
            pid=(None if payload.get("pid") is None else int(payload.get("pid"))),
            run_dir=(str(payload.get("run_dir")) if payload.get("run_dir") else None),
            summary_path=(str(payload.get("summary_path")) if payload.get("summary_path") else None),
            started_at=(str(payload.get("started_at")) if payload.get("started_at") else None),
            completed_at=(str(payload.get("completed_at")) if payload.get("completed_at") else None),
            metrics=(dict(payload.get("metrics") or {}) if payload.get("metrics") is not None else None),
            last_error=(str(payload.get("last_error")) if payload.get("last_error") else None),
        )


class WorkflowState:
    _ALLOWED_TRANSITIONS = {
        LaneStatus.PENDING: {LaneStatus.WAITING_RESOURCE, LaneStatus.RUNNING, LaneStatus.GATE_FAILED, LaneStatus.CANCELED},
        LaneStatus.WAITING_RESOURCE: {LaneStatus.WAITING_RESOURCE, LaneStatus.RUNNING, LaneStatus.GATE_FAILED, LaneStatus.CANCELED},
        LaneStatus.RUNNING: {LaneStatus.PENDING, LaneStatus.RUNNING, LaneStatus.PUBLISHABLE, LaneStatus.HELD, LaneStatus.GATE_FAILED, LaneStatus.INFRA_FAILED, LaneStatus.CANCELED},
        LaneStatus.INFRA_FAILED: {LaneStatus.PENDING, LaneStatus.INFRA_FAILED},
        LaneStatus.PUBLISHABLE: set(),
        LaneStatus.HELD: set(),
        LaneStatus.GATE_FAILED: set(),
        LaneStatus.CANCELED: set(),
    }

    def __init__(
        self,
        *,
        workflow_id: str,
        workflow_root: Path,
        started_at: str,
        lanes: Dict[str, LaneState],
    ) -> None:
        self.workflow_id = str(workflow_id)
        self.workflow_root = Path(workflow_root).resolve()
        self.started_at = str(started_at)
        self.updated_at = str(started_at)
        self.completed_at: Optional[str] = None
        self.lanes = dict(lanes)

    @property
    def path(self) -> Path:
        return self.workflow_root / "workflow_state.json"

    @classmethod
    def load(cls, workflow_root: Path, spec: WorkflowSpec) -> "WorkflowState":
        state_path = Path(workflow_root).resolve() / "workflow_state.json"
        if not state_path.exists():
            started_at = utc_now()
            lanes = {lane.lane_id: LaneState(lane_id=lane.lane_id, status=LaneStatus.PENDING) for lane in spec.lanes}
            state = cls(
                workflow_id=spec.workflow_id,
                workflow_root=workflow_root,
                started_at=started_at,
                lanes=lanes,
            )
            state.save()
            return state
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if str(payload.get("workflow_id") or "") != spec.workflow_id:
            raise ValueError("workflow_state.json workflow_id does not match spec")
        lane_payloads = dict(payload.get("lanes") or {})
        lane_ids = set(lane_payloads.keys())
        expected_ids = {lane.lane_id for lane in spec.lanes}
        if lane_ids != expected_ids:
            raise ValueError("workflow_state.json lane set does not match spec")
        state = cls(
            workflow_id=spec.workflow_id,
            workflow_root=workflow_root,
            started_at=str(payload.get("started_at") or utc_now()),
            lanes={lane_id: LaneState.from_dict(dict(lane_state)) for lane_id, lane_state in lane_payloads.items()},
        )
        state.updated_at = str(payload.get("updated_at") or state.started_at)
        state.completed_at = (str(payload.get("completed_at")) if payload.get("completed_at") else None)
        return state

    def save(self) -> None:
        self.workflow_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "workflow_id": self.workflow_id,
            "workflow_root": str(self.workflow_root),
            "started_at": self.started_at,
            "updated_at": utc_now(),
            "completed_at": self.completed_at,
            "lanes": {lane_id: lane.to_dict() for lane_id, lane in self.lanes.items()},
        }
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.path)
        self.updated_at = str(payload["updated_at"])

    def transition(self, lane_id: str, new_status: LaneStatus, **updates: Any) -> LaneState:
        lane = self.get(lane_id)
        if new_status != lane.status and new_status not in self._ALLOWED_TRANSITIONS[lane.status]:
            raise ValueError(f"illegal lane transition for {lane_id}: {lane.status.value} -> {new_status.value}")
        lane.status = new_status
        if "attempt" in updates:
            lane.attempt = int(updates["attempt"])
        if "pid" in updates:
            lane.pid = None if updates["pid"] is None else int(updates["pid"])
        if "run_dir" in updates:
            lane.run_dir = None if updates["run_dir"] is None else str(updates["run_dir"])
        if "summary_path" in updates:
            lane.summary_path = None if updates["summary_path"] is None else str(updates["summary_path"])
        if "started_at" in updates:
            lane.started_at = None if updates["started_at"] is None else str(updates["started_at"])
        if "completed_at" in updates:
            lane.completed_at = None if updates["completed_at"] is None else str(updates["completed_at"])
        if "metrics" in updates:
            lane.metrics = None if updates["metrics"] is None else dict(updates["metrics"])
        if "last_error" in updates:
            lane.last_error = None if updates["last_error"] is None else str(updates["last_error"])
        self.save()
        return lane

    def mark_completed(self, lane_id: str, *, status: LaneStatus, metrics: Optional[Dict[str, Any]], summary_path: Optional[Path], error: Optional[str]) -> LaneState:
        lane = self.transition(
            lane_id,
            status,
            completed_at=utc_now(),
            pid=None,
            summary_path=(None if summary_path is None else str(summary_path.resolve())),
            metrics=metrics,
            last_error=error,
        )
        if all(item.status.is_terminal for item in self.lanes.values()):
            self.completed_at = utc_now()
            self.save()
        return lane

    def get(self, lane_id: str) -> LaneState:
        if lane_id not in self.lanes:
            raise KeyError(f"unknown lane_id: {lane_id}")
        return self.lanes[lane_id]

    def lanes_by_status(self, status: LaneStatus) -> list[LaneState]:
        return [lane for lane in self.lanes.values() if lane.status == status]


__all__ = ["LaneState", "LaneStatus", "WorkflowState"]
