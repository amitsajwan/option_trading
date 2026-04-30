from __future__ import annotations

import itertools
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..contracts.manifests import load_and_resolve_manifest
from ..factory.spec import load_workflow_spec
from .spec import CampaignSpec, FamilySpec, LaneTemplate


_AXIS_ORDER = (
    "window_profile",
    "model_family",
    "stage2_feature_family",
    "stage2_policy_family",
    "stage3_policy_family",
    "recipe_catalog_family",
    "runtime_family",
)
_AXIS_SHORT = {
    "window_profile": "wp",
    "model_family": "mf",
    "stage2_feature_family": "s2f",
    "stage2_policy_family": "s2p",
    "stage3_policy_family": "s3p",
    "recipe_catalog_family": "rc",
    "runtime_family": "rt",
}
_FAMILY_AXIS_TO_GROUP = {
    "model_family": "model_families",
    "stage2_feature_family": "stage2_feature_families",
    "stage2_policy_family": "stage2_policy_families",
    "stage3_policy_family": "stage3_policy_families",
    "recipe_catalog_family": "recipe_catalog_families",
    "runtime_family": "runtime_families",
}


def _read_json_object(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = deepcopy(value)
    return result


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return normalized or "x"


def _relative_to(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


@dataclass(frozen=True)
class GeneratedLane:
    lane_id: str
    template_id: str
    selections: Dict[str, str]
    depends_on: tuple[str, ...]
    staged_manifest_path: Path
    grid_manifest_path: Path
    resource: Dict[str, Any]
    model_group: str
    profile_id: str
    model_bucket_url: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "lane_id": self.lane_id,
            "template_id": self.template_id,
            "selections": dict(self.selections),
            "depends_on": list(self.depends_on),
            "staged_manifest_path": str(self.staged_manifest_path),
            "grid_manifest_path": str(self.grid_manifest_path),
            "resource": dict(self.resource),
            "model_group": self.model_group,
            "profile_id": self.profile_id,
        }
        if self.model_bucket_url:
            payload["model_bucket_url"] = self.model_bucket_url
        return payload


@dataclass(frozen=True)
class CampaignExpansion:
    campaign_root: Path
    generated_workflow_path: Path
    generated_manifests_root: Path
    campaign_spec_path: Path
    campaign_expansion_path: Path
    generated_lanes: tuple[GeneratedLane, ...]
    workflow_payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "campaign_root": str(self.campaign_root),
            "generated_workflow_path": str(self.generated_workflow_path),
            "generated_manifests_root": str(self.generated_manifests_root),
            "campaign_spec_path": str(self.campaign_spec_path),
            "campaign_expansion_path": str(self.campaign_expansion_path),
            "generated_lane_count": len(self.generated_lanes),
            "generated_lanes": [lane.to_dict() for lane in self.generated_lanes],
        }


class CampaignGenerator:
    def __init__(self, spec: CampaignSpec, campaign_root: Path) -> None:
        self.spec = spec
        self.campaign_root = Path(campaign_root).resolve()
        self.generated_manifests_root = self.campaign_root / "generated_manifests"
        self._staged_root = self.generated_manifests_root / "staged"
        self._grid_root = self.generated_manifests_root / "grid"

    def _family_for_axis(self, axis: str, family_name: str) -> FamilySpec:
        return self.spec.families[_FAMILY_AXIS_TO_GROUP[axis]][family_name]

    def _template_axes(self, template: LaneTemplate) -> Dict[str, tuple[str, ...]]:
        return {
            axis_name: values
            for axis_name, values in template.selected_values().items()
            if values
        }

    def _iter_template_combinations(self, template: LaneTemplate) -> List[Dict[str, str]]:
        axes = self._template_axes(template)
        ordered_axes = [axis for axis in _AXIS_ORDER if axis in axes]
        combinations: List[Dict[str, str]] = []
        for values in itertools.product(*(axes[axis] for axis in ordered_axes)):
            selection = {axis: str(value) for axis, value in zip(ordered_axes, values)}
            if any(all(selection.get(axis) == value for axis, value in exclusion.items()) for exclusion in template.exclude_combinations):
                continue
            combinations.append(selection)
        if len(combinations) > template.max_generated_lanes:
            raise ValueError(
                f"template {template.template_id} expands to {len(combinations)} lanes which exceeds max_generated_lanes={template.max_generated_lanes}"
            )
        return combinations

    def _lane_id_for_selection(self, template_id: str, selection: Dict[str, str]) -> str:
        parts = [_slug(template_id)]
        for axis in _AXIS_ORDER:
            value = selection.get(axis)
            if not value:
                continue
            parts.append(f"{_AXIS_SHORT[axis]}_{_slug(value)}")
        return "__".join(parts)

    def _load_base_grid(self, path: Path) -> tuple[Dict[str, Any], Dict[str, Any], Path, Dict[str, Any]]:
        grid_raw = _read_json_object(path)
        grid_resolved = load_and_resolve_manifest(path, validate_paths=True)
        base_manifest_path = Path(str(((grid_resolved.get("inputs") or {}).get("base_manifest_path")))).resolve()
        base_manifest_raw = _read_json_object(base_manifest_path)
        return grid_raw, grid_resolved, base_manifest_path, base_manifest_raw

    def _apply_base_family(self, staged_manifest: Dict[str, Any], axis: str, family: FamilySpec) -> None:
        if axis == "model_family":
            catalog = staged_manifest.setdefault("catalog", {})
            existing = dict(catalog.get("models_by_stage") or {})
            for stage_name, models in family.payload["models_by_stage"].items():
                existing[stage_name] = list(models)
            catalog["models_by_stage"] = existing
            return
        if axis == "stage2_feature_family":
            catalog = staged_manifest.setdefault("catalog", {})
            feature_sets_by_stage = dict(catalog.get("feature_sets_by_stage") or {})
            feature_sets_by_stage["stage2"] = list(family.payload["feature_sets"])
            catalog["feature_sets_by_stage"] = feature_sets_by_stage
            return
        if axis == "stage2_policy_family":
            policy = staged_manifest.setdefault("policy", {})
            if family.payload.get("stage2_policy_id"):
                policy["stage2_policy_id"] = str(family.payload["stage2_policy_id"])
            if isinstance(family.payload.get("stage2"), dict):
                policy["stage2"] = _deep_merge(dict(policy.get("stage2") or {}), dict(family.payload["stage2"]))
            return
        if axis == "stage3_policy_family":
            policy = staged_manifest.setdefault("policy", {})
            if family.payload.get("stage3_policy_id"):
                policy["stage3_policy_id"] = str(family.payload["stage3_policy_id"])
            if isinstance(family.payload.get("stage3"), dict):
                policy["stage3"] = _deep_merge(dict(policy.get("stage3") or {}), dict(family.payload["stage3"]))
            return
        if axis == "recipe_catalog_family":
            staged_manifest.setdefault("catalog", {})["recipe_catalog_id"] = str(family.payload["recipe_catalog_id"])
            return
        if axis == "runtime_family":
            staged_manifest.setdefault("runtime", {})["block_expiry"] = bool(family.payload["block_expiry"])
            return
        raise ValueError(f"unsupported base-manifest family axis: {axis}")

    def _apply_grid_run_family(self, run_spec: Dict[str, Any], axis: str, family: FamilySpec) -> None:
        overrides = dict(run_spec.get("overrides") or {})
        if axis == "stage2_feature_family":
            catalog = dict(overrides.get("catalog") or {})
            feature_sets_by_stage = dict(catalog.get("feature_sets_by_stage") or {})
            feature_sets_by_stage["stage2"] = list(family.payload["feature_sets"])
            catalog["feature_sets_by_stage"] = feature_sets_by_stage
            overrides["catalog"] = catalog
        elif axis == "stage2_policy_family":
            policy = dict(overrides.get("policy") or {})
            if family.payload.get("stage2_policy_id"):
                policy["stage2_policy_id"] = str(family.payload["stage2_policy_id"])
            if isinstance(family.payload.get("stage2"), dict):
                policy["stage2"] = _deep_merge(dict(policy.get("stage2") or {}), dict(family.payload["stage2"]))
            overrides["policy"] = policy
        elif axis == "stage3_policy_family":
            policy = dict(overrides.get("policy") or {})
            if family.payload.get("stage3_policy_id"):
                policy["stage3_policy_id"] = str(family.payload["stage3_policy_id"])
            if isinstance(family.payload.get("stage3"), dict):
                policy["stage3"] = _deep_merge(dict(policy.get("stage3") or {}), dict(family.payload["stage3"]))
            overrides["policy"] = policy
        elif axis == "recipe_catalog_family":
            catalog = dict(overrides.get("catalog") or {})
            catalog["recipe_catalog_id"] = str(family.payload["recipe_catalog_id"])
            overrides["catalog"] = catalog
        elif axis == "runtime_family":
            runtime = dict(overrides.get("runtime") or {})
            runtime["block_expiry"] = bool(family.payload["block_expiry"])
            overrides["runtime"] = runtime
        else:
            raise ValueError(f"unsupported grid-run family axis: {axis}")
        run_spec["overrides"] = overrides

    def _build_generated_staged_manifest(
        self,
        *,
        template: LaneTemplate,
        selection: Dict[str, str],
        base_manifest_raw: Dict[str, Any],
        lane_id: str,
    ) -> Dict[str, Any]:
        staged_manifest = deepcopy(base_manifest_raw)
        staged_manifest.setdefault("inputs", {})["parquet_root"] = str(self.spec.inputs.parquet_root)
        staged_manifest.setdefault("inputs", {})["support_dataset"] = self.spec.inputs.support_dataset
        staged_manifest.setdefault("outputs", {})["run_name"] = lane_id
        window_profile = self.spec.window_profiles[selection["window_profile"]]
        staged_manifest["windows"] = deepcopy(window_profile.windows)
        for axis in _AXIS_ORDER:
            family_name = selection.get(axis)
            if not family_name or axis == "window_profile":
                continue
            family = self._family_for_axis(axis, family_name)
            if family.target == "base_manifest":
                self._apply_base_family(staged_manifest, axis, family)
        return staged_manifest

    def _build_generated_grid_manifest(
        self,
        *,
        template: LaneTemplate,
        selection: Dict[str, str],
        grid_raw: Dict[str, Any],
        generated_staged_manifest_path: Path,
        lane_id: str,
    ) -> Dict[str, Any]:
        grid_manifest = deepcopy(grid_raw)
        grid_manifest.setdefault("inputs", {})["base_manifest_path"] = str(generated_staged_manifest_path)
        grid_manifest.setdefault("outputs", {})["run_name"] = lane_id
        runs = list(((grid_manifest.get("grid") or {}).get("runs") or []))
        for run_spec in runs:
            run_id = str(run_spec.get("run_id") or "").strip()
            for axis in _AXIS_ORDER:
                family_name = selection.get(axis)
                if not family_name or axis == "window_profile":
                    continue
                family = self._family_for_axis(axis, family_name)
                if family.target != "grid_runs":
                    continue
                if run_id not in family.run_id_selectors:
                    continue
                self._apply_grid_run_family(run_spec, axis, family)
            overrides = dict(run_spec.get("overrides") or {})
            outputs = dict(overrides.get("outputs") or {})
            outputs["run_name"] = f"{lane_id}__{run_id}"
            overrides["outputs"] = outputs
            run_spec["overrides"] = overrides
        return grid_manifest

    def _matching_dependency_lane_ids(
        self,
        *,
        template: LaneTemplate,
        selection: Dict[str, str],
        template_expansions: Dict[str, List[GeneratedLane]],
    ) -> tuple[str, ...]:
        dependencies: List[str] = []
        template_axes = self._template_axes(template)
        for dependency_template_id in template.depends_on_templates:
            candidates = template_expansions.get(dependency_template_id) or []
            dependency_template = next(item for item in self.spec.lane_templates if item.template_id == dependency_template_id)
            shared_axes = sorted(set(template_axes) & set(self._template_axes(dependency_template)))
            matched = []
            for candidate in candidates:
                if all(candidate.selections.get(axis) == selection.get(axis) for axis in shared_axes):
                    matched.append(candidate)
            if len(matched) != 1:
                raise ValueError(
                    f"template {template.template_id} selection {selection} matched {len(matched)} dependency lanes in {dependency_template_id}; expected exactly 1"
                )
            dependencies.append(matched[0].lane_id)
        return tuple(dependencies)

    def _build_workflow_payload(self, lanes: Iterable[GeneratedLane]) -> Dict[str, Any]:
        return {
            "workflow_id": self.spec.campaign_id,
            "inputs": self.spec.inputs.to_dict(),
            "lanes": [
                {
                    "lane_id": lane.lane_id,
                    "lane_kind": "staged_grid",
                    "runner_mode": "research",
                    "config_path": str(lane.grid_manifest_path),
                    "depends_on": list(lane.depends_on),
                    "resource": dict(lane.resource),
                    "model_group": lane.model_group,
                    "profile_id": lane.profile_id,
                    **({"model_bucket_url": lane.model_bucket_url} if lane.model_bucket_url else {}),
                }
                for lane in lanes
            ],
            "execution": {
                "poll_interval_seconds": self.spec.execution_defaults.poll_interval_seconds,
                "infra_max_attempts": self.spec.execution_defaults.infra_max_attempts,
            },
            "resource_budget": {
                "total_cores": self.spec.execution_defaults.total_cores,
                "total_memory_gb": self.spec.execution_defaults.total_memory_gb,
            },
            "selection": {
                "ranking_strategy": self.spec.execution_defaults.ranking_strategy,
                "stop_on_first_publishable": self.spec.execution_defaults.stop_on_first_publishable,
            },
        }

    def generate(self) -> CampaignExpansion:
        self.campaign_root.mkdir(parents=True, exist_ok=True)
        self.generated_manifests_root.mkdir(parents=True, exist_ok=True)
        template_expansions: Dict[str, List[GeneratedLane]] = {}
        all_generated_lanes: List[GeneratedLane] = []

        for template in self.spec.lane_templates:
            grid_raw, _grid_resolved, _base_manifest_path, base_manifest_raw = self._load_base_grid(template.base_grid_path)
            generated_for_template: List[GeneratedLane] = []
            for selection in self._iter_template_combinations(template):
                lane_id = self._lane_id_for_selection(template.template_id, selection)
                depends_on = self._matching_dependency_lane_ids(
                    template=template,
                    selection=selection,
                    template_expansions=template_expansions,
                )
                staged_manifest = self._build_generated_staged_manifest(
                    template=template,
                    selection=selection,
                    base_manifest_raw=base_manifest_raw,
                    lane_id=lane_id,
                )
                staged_manifest_path = self._staged_root / f"{lane_id}.json"
                _write_json(staged_manifest_path, staged_manifest)
                load_and_resolve_manifest(staged_manifest_path, validate_paths=True)

                grid_manifest = self._build_generated_grid_manifest(
                    template=template,
                    selection=selection,
                    grid_raw=grid_raw,
                    generated_staged_manifest_path=staged_manifest_path,
                    lane_id=lane_id,
                )
                grid_manifest_path = self._grid_root / f"{lane_id}.json"
                _write_json(grid_manifest_path, grid_manifest)
                load_and_resolve_manifest(grid_manifest_path, validate_paths=True)

                generated_lane = GeneratedLane(
                    lane_id=lane_id,
                    template_id=template.template_id,
                    selections=dict(selection),
                    depends_on=depends_on,
                    staged_manifest_path=staged_manifest_path,
                    grid_manifest_path=grid_manifest_path,
                    resource=template.resource.to_dict() if template.resource is not None else {"cores": 1, "memory_gb": 1.0},
                    model_group=(template.model_group or self.spec.execution_defaults.model_group),
                    profile_id=(template.profile_id or self.spec.execution_defaults.profile_id),
                    model_bucket_url=(template.model_bucket_url or self.spec.execution_defaults.model_bucket_url),
                )
                generated_for_template.append(generated_lane)
                all_generated_lanes.append(generated_lane)
            template_expansions[template.template_id] = generated_for_template

        if self.spec.campaign_max_lanes is not None and len(all_generated_lanes) > self.spec.campaign_max_lanes:
            raise ValueError(
                f"campaign {self.spec.campaign_id} expands to {len(all_generated_lanes)} lanes which exceeds campaign_max_lanes={self.spec.campaign_max_lanes}"
            )

        workflow_payload = self._build_workflow_payload(all_generated_lanes)
        generated_workflow_path = self.campaign_root / "generated_workflow.json"
        _write_json(generated_workflow_path, workflow_payload)
        load_workflow_spec(generated_workflow_path)

        campaign_spec_path = _write_json(self.campaign_root / "campaign_spec_resolved.json", self.spec.to_dict())
        campaign_expansion_payload = {
            "campaign_id": self.spec.campaign_id,
            "generated_lane_count": len(all_generated_lanes),
            "generated_manifests_root": str(self.generated_manifests_root),
            "generated_workflow_path": str(generated_workflow_path),
            "generated_lanes": [lane.to_dict() for lane in all_generated_lanes],
        }
        campaign_expansion_path = _write_json(self.campaign_root / "campaign_expansion.json", campaign_expansion_payload)

        return CampaignExpansion(
            campaign_root=self.campaign_root,
            generated_workflow_path=generated_workflow_path,
            generated_manifests_root=self.generated_manifests_root,
            campaign_spec_path=campaign_spec_path,
            campaign_expansion_path=campaign_expansion_path,
            generated_lanes=tuple(all_generated_lanes),
            workflow_payload=workflow_payload,
        )


__all__ = ["CampaignExpansion", "CampaignGenerator", "GeneratedLane"]
