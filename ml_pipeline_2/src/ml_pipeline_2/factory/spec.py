from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from ..contracts.manifests import STAGED_GRID_KIND, STAGED_KIND, load_and_resolve_manifest


LaneKind = Literal["staged_grid", "staged_manifest"]
RunnerMode = Literal["research", "release"]


def _resolve_path(value: str, *, manifest_dir: Path) -> Path:
    path = Path(str(value).strip())
    if not path.is_absolute():
        path = (manifest_dir / path).resolve()
    return path.resolve()


def _read_json_object(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _topological_order(lanes: Dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    permanent: set[str] = set()
    temporary: set[str] = set()
    ordered: list[str] = []

    def visit(node: str) -> None:
        if node in permanent:
            return
        if node in temporary:
            raise ValueError(f"cycle detected in workflow lanes at: {node}")
        temporary.add(node)
        for dependency in lanes[node]:
            visit(dependency)
        temporary.remove(node)
        permanent.add(node)
        ordered.append(node)

    for lane_id in lanes:
        visit(lane_id)
    return tuple(ordered)


@dataclass(frozen=True)
class ResourceSpec:
    cores: int
    memory_gb: float

    def to_dict(self) -> Dict[str, Any]:
        return {"cores": int(self.cores), "memory_gb": float(self.memory_gb)}


@dataclass(frozen=True)
class LaneSpec:
    lane_id: str
    lane_kind: LaneKind
    config_path: Path
    runner_mode: RunnerMode
    depends_on: tuple[str, ...]
    resource: ResourceSpec
    model_group: Optional[str] = None
    profile_id: Optional[str] = None
    model_bucket_url: Optional[str] = None

    @property
    def summary_filename(self) -> str:
        return "grid_summary.json" if self.lane_kind == "staged_grid" else "summary.json"

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "lane_id": self.lane_id,
            "lane_kind": self.lane_kind,
            "config_path": str(self.config_path),
            "runner_mode": self.runner_mode,
            "depends_on": list(self.depends_on),
            "resource": self.resource.to_dict(),
        }
        if self.model_group:
            payload["model_group"] = self.model_group
        if self.profile_id:
            payload["profile_id"] = self.profile_id
        if self.model_bucket_url:
            payload["model_bucket_url"] = self.model_bucket_url
        return payload


@dataclass(frozen=True)
class ExecutionConfig:
    poll_interval_seconds: float
    infra_max_attempts: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "poll_interval_seconds": float(self.poll_interval_seconds),
            "infra_max_attempts": int(self.infra_max_attempts),
        }


@dataclass(frozen=True)
class ResourceBudgetConfig:
    total_cores: int
    total_memory_gb: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_cores": int(self.total_cores),
            "total_memory_gb": float(self.total_memory_gb),
        }


@dataclass(frozen=True)
class SelectionConfig:
    ranking_strategy: str
    stop_on_first_publishable: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ranking_strategy": str(self.ranking_strategy),
            "stop_on_first_publishable": bool(self.stop_on_first_publishable),
        }


@dataclass(frozen=True)
class WorkflowInputs:
    parquet_root: Path
    support_dataset: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parquet_root": str(self.parquet_root),
            "support_dataset": self.support_dataset,
        }


@dataclass(frozen=True)
class WorkflowSpec:
    workflow_id: str
    manifest_path: Path
    inputs: WorkflowInputs
    lanes: tuple[LaneSpec, ...]
    execution: ExecutionConfig
    resource_budget: ResourceBudgetConfig
    selection: SelectionConfig

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "manifest_path": str(self.manifest_path),
            "inputs": self.inputs.to_dict(),
            "lanes": [lane.to_dict() for lane in self.lanes],
            "execution": self.execution.to_dict(),
            "resource_budget": self.resource_budget.to_dict(),
            "selection": self.selection.to_dict(),
        }

    @property
    def lane_map(self) -> Dict[str, LaneSpec]:
        return {lane.lane_id: lane for lane in self.lanes}


def load_workflow_spec(path: Path) -> WorkflowSpec:
    manifest_path = Path(path).resolve()
    payload = _read_json_object(manifest_path)
    manifest_dir = manifest_path.parent

    workflow_id = str(payload.get("workflow_id") or "").strip()
    if not workflow_id:
        raise ValueError("workflow_id must be set")

    inputs_payload = dict(payload.get("inputs") or {})
    parquet_root = _resolve_path(str(inputs_payload.get("parquet_root") or ""), manifest_dir=manifest_dir)
    support_dataset = str(inputs_payload.get("support_dataset") or "").strip()
    if not support_dataset:
        raise ValueError("inputs.support_dataset must be set")
    dataset_root = parquet_root / support_dataset
    if not dataset_root.exists():
        raise ValueError(f"workflow dataset root does not exist: {dataset_root}")

    execution_payload = dict(payload.get("execution") or {})
    poll_interval_seconds = float(execution_payload.get("poll_interval_seconds", 30.0))
    if poll_interval_seconds < 0:
        raise ValueError("execution.poll_interval_seconds must be >= 0")
    infra_max_attempts = int(execution_payload.get("infra_max_attempts", 2))
    if infra_max_attempts < 1:
        raise ValueError("execution.infra_max_attempts must be >= 1")

    resource_budget_payload = dict(payload.get("resource_budget") or {})
    total_cores = int(resource_budget_payload.get("total_cores", max(1, int(os.cpu_count() or 1))))
    total_memory_gb = float(resource_budget_payload.get("total_memory_gb", 1.0))
    if total_cores < 1:
        raise ValueError("resource_budget.total_cores must be >= 1")
    if total_memory_gb <= 0:
        raise ValueError("resource_budget.total_memory_gb must be > 0")

    selection_payload = dict(payload.get("selection") or {})
    ranking_strategy = str(selection_payload.get("ranking_strategy") or "publishable_economics_v1").strip()
    if not ranking_strategy:
        raise ValueError("selection.ranking_strategy must be set")
    stop_on_first_publishable = bool(selection_payload.get("stop_on_first_publishable", False))

    seen_ids: set[str] = set()
    dependency_map: Dict[str, tuple[str, ...]] = {}
    lane_specs: list[LaneSpec] = []
    for index, raw_lane in enumerate(list(payload.get("lanes") or []), start=1):
        if not isinstance(raw_lane, dict):
            raise ValueError(f"lanes[{index}] must be an object")
        lane_id = str(raw_lane.get("lane_id") or "").strip()
        if not lane_id:
            raise ValueError(f"lanes[{index}].lane_id must be set")
        if lane_id in seen_ids:
            raise ValueError(f"duplicate lane_id: {lane_id}")
        seen_ids.add(lane_id)

        lane_kind = str(raw_lane.get("lane_kind") or "").strip()
        if lane_kind not in {"staged_grid", "staged_manifest"}:
            raise ValueError(f"lanes[{index}].lane_kind must be one of ['staged_grid', 'staged_manifest']")

        config_value = str(raw_lane.get("config_path") or "").strip()
        if not config_value:
            raise ValueError(f"lanes[{index}].config_path must be set")
        config_path = _resolve_path(config_value, manifest_dir=manifest_dir)
        if not config_path.exists():
            raise ValueError(f"lanes[{index}].config_path does not exist: {config_path}")

        runner_mode = str(raw_lane.get("runner_mode") or "research").strip().lower()
        if runner_mode not in {"research", "release"}:
            raise ValueError(f"lanes[{index}].runner_mode must be one of ['research', 'release']")
        if lane_kind == "staged_grid" and runner_mode != "research":
            raise ValueError(f"lanes[{index}] staged_grid only supports runner_mode='research'")

        try:
            resolved_config = load_and_resolve_manifest(config_path, validate_paths=True)
        except Exception as exc:
            raise ValueError(f"lanes[{index}].config_path failed manifest resolution: {exc}") from exc
        config_kind = str(resolved_config.get("experiment_kind") or "").strip()
        if lane_kind == "staged_grid" and config_kind != STAGED_GRID_KIND:
            raise ValueError(f"lanes[{index}].config_path must point to a staged grid manifest")
        if lane_kind == "staged_manifest" and config_kind != STAGED_KIND:
            raise ValueError(f"lanes[{index}].config_path must point to a staged manifest")

        depends_on = tuple(str(item).strip() for item in list(raw_lane.get("depends_on") or []) if str(item).strip())
        resource_payload = dict(raw_lane.get("resource") or {})
        cores = int(resource_payload.get("cores", 0))
        memory_gb = float(resource_payload.get("memory_gb", 0.0))
        if cores < 1:
            raise ValueError(f"lanes[{index}].resource.cores must be >= 1")
        if memory_gb <= 0:
            raise ValueError(f"lanes[{index}].resource.memory_gb must be > 0")

        model_group = str(raw_lane.get("model_group") or "").strip() or None
        profile_id = str(raw_lane.get("profile_id") or "").strip() or None
        model_bucket_url = str(raw_lane.get("model_bucket_url") or "").strip() or None
        if lane_kind == "staged_grid" or runner_mode == "release":
            if not model_group:
                raise ValueError(f"lanes[{index}] requires model_group")
            if not profile_id:
                raise ValueError(f"lanes[{index}] requires profile_id")

        dependency_map[lane_id] = depends_on
        lane_specs.append(
            LaneSpec(
                lane_id=lane_id,
                lane_kind=lane_kind,  # type: ignore[arg-type]
                config_path=config_path,
                runner_mode=runner_mode,  # type: ignore[arg-type]
                depends_on=depends_on,
                resource=ResourceSpec(cores=cores, memory_gb=memory_gb),
                model_group=model_group,
                profile_id=profile_id,
                model_bucket_url=model_bucket_url,
            )
        )

    if not lane_specs:
        raise ValueError("lanes must not be empty")

    for lane_id, depends_on in dependency_map.items():
        unknown = [item for item in depends_on if item not in dependency_map]
        if unknown:
            raise ValueError(f"lane {lane_id} depends on unknown lanes: {unknown}")
    ordered_ids = _topological_order(dependency_map)
    order_index = {lane_id: idx for idx, lane_id in enumerate(ordered_ids)}
    lane_specs.sort(key=lambda lane: (order_index[lane.lane_id], lane.lane_id))

    return WorkflowSpec(
        workflow_id=workflow_id,
        manifest_path=manifest_path,
        inputs=WorkflowInputs(parquet_root=parquet_root, support_dataset=support_dataset),
        lanes=tuple(lane_specs),
        execution=ExecutionConfig(
            poll_interval_seconds=poll_interval_seconds,
            infra_max_attempts=infra_max_attempts,
        ),
        resource_budget=ResourceBudgetConfig(total_cores=total_cores, total_memory_gb=total_memory_gb),
        selection=SelectionConfig(
            ranking_strategy=ranking_strategy,
            stop_on_first_publishable=stop_on_first_publishable,
        ),
    )


__all__ = [
    "ExecutionConfig",
    "LaneSpec",
    "ResourceBudgetConfig",
    "ResourceSpec",
    "SelectionConfig",
    "WorkflowInputs",
    "WorkflowSpec",
    "load_workflow_spec",
]
