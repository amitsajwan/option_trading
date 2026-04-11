from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

from ..experiment_control.state import utc_now
from .budget import ResourceBudget
from .launcher import LaneLauncher
from .monitor import LaneOutcome, classify_lane_result
from .selector import select_winner
from .spec import LaneSpec, WorkflowSpec
from .state import LaneStatus, WorkflowState


def resolve_workflow_root(spec: WorkflowSpec, output_root: Optional[Path]) -> Path:
    if output_root is None:
        return (Path("ml_pipeline_2") / "artifacts" / "factory_runs" / spec.workflow_id).resolve()
    explicit = Path(output_root).resolve()
    if explicit.name == spec.workflow_id:
        return explicit
    return (explicit / spec.workflow_id).resolve()


class WorkflowRunner:
    def __init__(self, spec: WorkflowSpec, workflow_root: Path) -> None:
        self.spec = spec
        self.workflow_root = Path(workflow_root).resolve()
        self.launcher = LaneLauncher()
        self.state = WorkflowState.load(self.workflow_root, spec)
        self.budget = ResourceBudget(
            total_cores=self.spec.resource_budget.total_cores,
            total_memory_gb=self.spec.resource_budget.total_memory_gb,
        )
        self._lanes_root = self.workflow_root / "lanes"
        self._write_resolved_spec()
        self._reap_orphaned_running_lanes()
        self._rebuild_budget_from_state()

    def _write_resolved_spec(self) -> None:
        self.workflow_root.mkdir(parents=True, exist_ok=True)
        self._lanes_root.mkdir(parents=True, exist_ok=True)
        resolved_path = self.workflow_root / "workflow_spec_resolved.json"
        if not resolved_path.exists():
            resolved_path.write_text(json.dumps(self.spec.to_dict(), indent=2), encoding="utf-8")

    def _rebuild_budget_from_state(self) -> None:
        for lane in self.spec.lanes:
            lane_state = self.state.get(lane.lane_id)
            if lane_state.status == LaneStatus.RUNNING:
                try:
                    self.budget.acquire(lane.lane_id, lane.resource.cores, lane.resource.memory_gb)
                except ValueError:
                    continue

    def _publishable_found(self) -> bool:
        return any(self.state.get(lane.lane_id).status == LaneStatus.PUBLISHABLE for lane in self.spec.lanes)

    def _lane_root(self, lane: LaneSpec) -> Path:
        sequence = next(index for index, item in enumerate(self.spec.lanes, start=1) if item.lane_id == lane.lane_id)
        return self._lanes_root / f"{sequence:02d}_{lane.lane_id}"

    def _lane_run_root(self, lane: LaneSpec) -> Path:
        return self._lane_root(lane) / "runner_output"

    def _dependency_statuses(self, lane: LaneSpec) -> list[LaneStatus]:
        return [self.state.get(dependency).status for dependency in lane.depends_on]

    def _has_failed_dependency(self, lane: LaneSpec) -> bool:
        return any(status in {LaneStatus.GATE_FAILED, LaneStatus.INFRA_FAILED} for status in self._dependency_statuses(lane))

    def _dependencies_ready(self, lane: LaneSpec) -> bool:
        return all(self.state.get(dependency).status.is_terminal for dependency in lane.depends_on)

    def _retry_or_finalize_infra_failure(self, lane: LaneSpec, lane_state, error: Optional[str], summary_path: Optional[Path]) -> None:
        if lane_state.attempt < self.spec.execution.infra_max_attempts:
            self.state.transition(
                lane.lane_id,
                LaneStatus.PENDING,
                attempt=(lane_state.attempt + 1),
                pid=None,
                completed_at=None,
                summary_path=(None if summary_path is None else str(summary_path.resolve())),
                metrics=None,
                last_error=error,
            )
            return
        self.state.mark_completed(
            lane.lane_id,
            status=LaneStatus.INFRA_FAILED,
            metrics=None,
            summary_path=summary_path,
            error=error,
        )

    def _apply_lane_outcome(
        self,
        lane: LaneSpec,
        lane_state,
        *,
        outcome: LaneOutcome,
        metrics: Optional[Dict[str, Any]],
        summary_path: Optional[Path],
        error: Optional[str],
        release_budget: bool,
    ) -> None:
        if release_budget:
            self.budget.release(lane.lane_id)
        if outcome == LaneOutcome.PUBLISHABLE:
            self.state.mark_completed(
                lane.lane_id,
                status=LaneStatus.PUBLISHABLE,
                metrics=metrics,
                summary_path=summary_path,
                error=None,
            )
            return
        if outcome == LaneOutcome.HELD:
            self.state.mark_completed(
                lane.lane_id,
                status=LaneStatus.HELD,
                metrics=metrics,
                summary_path=summary_path,
                error=error,
            )
            return
        if outcome == LaneOutcome.GATE_FAILED:
            self.state.mark_completed(
                lane.lane_id,
                status=LaneStatus.GATE_FAILED,
                metrics=None,
                summary_path=summary_path,
                error=error,
            )
            return
        self._retry_or_finalize_infra_failure(
            lane,
            lane_state,
            error,
            summary_path,
        )

    def _reap_orphaned_running_lanes(self) -> None:
        for lane in self.spec.lanes:
            lane_state = self.state.get(lane.lane_id)
            if lane_state.status != LaneStatus.RUNNING:
                continue
            if self.launcher.is_alive(lane_state.pid):
                continue
            run_dir = Path(str(lane_state.run_dir or self._lane_run_root(lane))).resolve()
            exit_code = self.launcher.exit_code(lane_state.pid)
            outcome, metrics, resolved_summary_path, error = classify_lane_result(
                lane,
                run_dir,
                exit_code=exit_code,
            )
            self._apply_lane_outcome(
                lane,
                lane_state,
                outcome=outcome,
                metrics=metrics,
                summary_path=resolved_summary_path,
                error=error,
                release_budget=False,
            )

    def _poll_running_lanes(self) -> None:
        for lane in self.spec.lanes:
            lane_state = self.state.get(lane.lane_id)
            if lane_state.status != LaneStatus.RUNNING:
                continue
            run_dir = Path(str(lane_state.run_dir or self._lane_run_root(lane))).resolve()
            summary_path = run_dir / lane.summary_filename
            alive = self.launcher.is_alive(lane_state.pid)
            if alive and not summary_path.exists():
                continue
            exit_code = self.launcher.exit_code(lane_state.pid)
            outcome, metrics, resolved_summary_path, error = classify_lane_result(
                lane,
                run_dir,
                exit_code=exit_code,
            )
            self._apply_lane_outcome(
                lane,
                lane_state,
                outcome=outcome,
                metrics=metrics,
                summary_path=resolved_summary_path,
                error=error,
                release_budget=True,
            )

    def _cancel_remaining_lanes(self) -> None:
        reason = "canceled_after_publishable_found"
        for lane in self.spec.lanes:
            lane_state = self.state.get(lane.lane_id)
            if lane_state.status == LaneStatus.RUNNING:
                self.launcher.terminate(lane_state.pid)
                self.budget.release(lane.lane_id)
                self.state.mark_completed(
                    lane.lane_id,
                    status=LaneStatus.CANCELED,
                    metrics=None,
                    summary_path=(None if lane_state.summary_path is None else Path(lane_state.summary_path)),
                    error=reason,
                )
            elif lane_state.status in {LaneStatus.PENDING, LaneStatus.WAITING_RESOURCE}:
                self.state.mark_completed(
                    lane.lane_id,
                    status=LaneStatus.CANCELED,
                    metrics=None,
                    summary_path=None,
                    error=reason,
                )

    def _launch_ready_lanes(self) -> None:
        if self.spec.selection.stop_on_first_publishable and self._publishable_found():
            return
        for lane in self.spec.lanes:
            lane_state = self.state.get(lane.lane_id)
            if lane_state.status not in {LaneStatus.PENDING, LaneStatus.WAITING_RESOURCE}:
                continue
            if not self._dependencies_ready(lane):
                continue
            if self._has_failed_dependency(lane):
                self.state.mark_completed(
                    lane.lane_id,
                    status=LaneStatus.GATE_FAILED,
                    metrics=None,
                    summary_path=None,
                    error="dependency_pruned",
                )
                continue
            if not self.budget.can_afford(lane.resource.cores, lane.resource.memory_gb):
                self.state.transition(lane.lane_id, LaneStatus.WAITING_RESOURCE)
                continue
            lane_root = self._lane_root(lane)
            self.budget.acquire(lane.lane_id, lane.resource.cores, lane.resource.memory_gb)
            launch_result = self.launcher.launch(lane, lane_root=lane_root)
            self.state.transition(
                lane.lane_id,
                LaneStatus.RUNNING,
                pid=launch_result.pid,
                run_dir=str(launch_result.runner_output_root.resolve()),
                started_at=(lane_state.started_at or utc_now()),
                last_error=None,
            )

    def _should_stop(self) -> bool:
        if self.spec.selection.stop_on_first_publishable and self._publishable_found():
            if not any(
                self.state.get(lane.lane_id).status in {LaneStatus.PENDING, LaneStatus.WAITING_RESOURCE, LaneStatus.RUNNING}
                for lane in self.spec.lanes
            ):
                return True
        return not any(
            self.state.get(lane.lane_id).status in {LaneStatus.PENDING, LaneStatus.WAITING_RESOURCE, LaneStatus.RUNNING}
            for lane in self.spec.lanes
        )

    def _candidate_rows(self) -> list[Dict[str, Any]]:
        rows: list[Dict[str, Any]] = []
        for lane in self.spec.lanes:
            lane_state = self.state.get(lane.lane_id)
            if lane_state.status == LaneStatus.PUBLISHABLE and isinstance(lane_state.metrics, dict):
                rows.append({"lane_id": lane.lane_id, **lane_state.metrics})
        return rows

    def _held_rows(self) -> list[Dict[str, Any]]:
        rows: list[Dict[str, Any]] = []
        for lane in self.spec.lanes:
            lane_state = self.state.get(lane.lane_id)
            if lane_state.status == LaneStatus.HELD and isinstance(lane_state.metrics, dict):
                rows.append({"lane_id": lane.lane_id, **lane_state.metrics})
        return rows

    def _blocking_reason_rollup(self) -> Dict[str, Any]:
        counts: Counter[str] = Counter()
        for lane in self.spec.lanes:
            lane_state = self.state.get(lane.lane_id)
            if lane_state.status in {LaneStatus.PUBLISHABLE, LaneStatus.CANCELED}:
                continue
            reason = str(lane_state.last_error or "").strip()
            if reason:
                counts[reason] += 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return {
            "dominant_blocking_reason": (None if not ordered else ordered[0][0]),
            "blocking_reason_counts": [{"reason": reason, "count": count} for reason, count in ordered],
        }

    def _write_workflow_result(self) -> Dict[str, Any]:
        winner = select_winner(self._candidate_rows(), strategy=self.spec.selection.ranking_strategy)
        best_nonpublishable = select_winner(self._held_rows(), strategy=self.spec.selection.ranking_strategy)
        winner_lane_state = None if winner is None else self.state.get(str(winner["lane_id"]))
        status_counts = Counter(self.state.get(lane.lane_id).status.value for lane in self.spec.lanes)
        blocking_rollup = self._blocking_reason_rollup()
        result = {
            "workflow_id": self.spec.workflow_id,
            "status": ("publishable_found" if winner is not None else "no_publishable_candidate"),
            "started_at": self.state.started_at,
            "completed_at": (self.state.completed_at or utc_now()),
            "winner_lane_id": (None if winner is None else str(winner["lane_id"])),
            "winner_run_dir": (None if winner_lane_state is None else winner_lane_state.run_dir),
            "winner_summary_path": (None if winner_lane_state is None else winner_lane_state.summary_path),
            "winner_metrics": (None if winner is None else {key: value for key, value in winner.items() if key != "lane_id"}),
            "publishable_candidates": [lane.lane_id for lane in self.spec.lanes if self.state.get(lane.lane_id).status == LaneStatus.PUBLISHABLE],
            "held_candidates": [lane.lane_id for lane in self.spec.lanes if self.state.get(lane.lane_id).status == LaneStatus.HELD],
            "canceled_lanes": [lane.lane_id for lane in self.spec.lanes if self.state.get(lane.lane_id).status == LaneStatus.CANCELED],
            "failed_lanes": [
                {
                    "lane_id": lane.lane_id,
                    "status": self.state.get(lane.lane_id).status.value,
                    "last_error": self.state.get(lane.lane_id).last_error,
                }
                for lane in self.spec.lanes
                if self.state.get(lane.lane_id).status in {LaneStatus.GATE_FAILED, LaneStatus.INFRA_FAILED}
            ],
            "status_counts": dict(sorted(status_counts.items())),
            "dominant_blocking_reason": blocking_rollup["dominant_blocking_reason"],
            "blocking_reason_counts": blocking_rollup["blocking_reason_counts"],
            "best_nonpublishable_candidate": (
                None
                if best_nonpublishable is None
                else {key: value for key, value in best_nonpublishable.items()}
            ),
            "lane_summary": [
                {
                    "lane_id": lane.lane_id,
                    "status": self.state.get(lane.lane_id).status.value,
                    "attempts": self.state.get(lane.lane_id).attempt,
                    "run_dir": self.state.get(lane.lane_id).run_dir,
                }
                for lane in self.spec.lanes
            ],
        }
        result_path = self.workflow_root / "workflow_result.json"
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def run(self) -> Dict[str, Any]:
        while True:
            self._poll_running_lanes()
            if self.spec.selection.stop_on_first_publishable and self._publishable_found():
                self._cancel_remaining_lanes()
            self._launch_ready_lanes()
            if self._should_stop():
                break
            time.sleep(self.spec.execution.poll_interval_seconds)
        self.state.completed_at = utc_now()
        self.state.save()
        return self._write_workflow_result()


__all__ = ["WorkflowRunner", "resolve_workflow_root"]
