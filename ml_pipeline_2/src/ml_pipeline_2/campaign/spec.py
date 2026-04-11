from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from ..contracts.manifests import STAGED_GRID_KIND, load_and_resolve_manifest
from ..factory.spec import ResourceSpec


FamilyTarget = Literal["base_manifest", "grid_runs"]

_WINDOW_KEYS = ("research_train", "research_valid", "full_model", "final_holdout")
_AXIS_TO_TEMPLATE_FIELD = {
    "window_profile": "window_profiles",
    "model_family": "model_families",
    "stage2_feature_family": "stage2_feature_families",
    "stage2_policy_family": "stage2_policy_families",
    "stage3_policy_family": "stage3_policy_families",
    "recipe_catalog_family": "recipe_catalog_families",
    "runtime_family": "runtime_families",
}
_FAMILY_GROUPS = {
    "model_families": "model_family",
    "stage2_feature_families": "stage2_feature_family",
    "stage2_policy_families": "stage2_policy_family",
    "stage3_policy_families": "stage3_policy_family",
    "recipe_catalog_families": "recipe_catalog_family",
    "runtime_families": "runtime_family",
}


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


def _topological_order(items: Dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    permanent: set[str] = set()
    temporary: set[str] = set()
    ordered: list[str] = []

    def visit(node: str) -> None:
        if node in permanent:
            return
        if node in temporary:
            raise ValueError(f"cycle detected in lane_templates at: {node}")
        temporary.add(node)
        for dependency in items[node]:
            visit(dependency)
        temporary.remove(node)
        permanent.add(node)
        ordered.append(node)

    for node in items:
        visit(node)
    return tuple(ordered)


@dataclass(frozen=True)
class CampaignInputs:
    parquet_root: Path
    support_dataset: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parquet_root": str(self.parquet_root),
            "support_dataset": self.support_dataset,
        }


@dataclass(frozen=True)
class ExecutionDefaults:
    poll_interval_seconds: float
    infra_max_attempts: int
    total_cores: int
    total_memory_gb: float
    ranking_strategy: str
    stop_on_first_publishable: bool
    model_group: str
    profile_id: str
    model_bucket_url: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "poll_interval_seconds": float(self.poll_interval_seconds),
            "infra_max_attempts": int(self.infra_max_attempts),
            "total_cores": int(self.total_cores),
            "total_memory_gb": float(self.total_memory_gb),
            "ranking_strategy": self.ranking_strategy,
            "stop_on_first_publishable": bool(self.stop_on_first_publishable),
            "model_group": self.model_group,
            "profile_id": self.profile_id,
        }
        if self.model_bucket_url:
            payload["model_bucket_url"] = self.model_bucket_url
        return payload


@dataclass(frozen=True)
class WindowProfile:
    name: str
    windows: Dict[str, Dict[str, str]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "windows": json.loads(json.dumps(self.windows)),
        }


@dataclass(frozen=True)
class FamilySpec:
    axis: str
    name: str
    target: FamilyTarget
    payload: Dict[str, Any]
    run_id_selectors: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "name": self.name,
            "target": self.target,
            **json.loads(json.dumps(self.payload)),
        }
        if self.run_id_selectors:
            payload["run_id_selectors"] = list(self.run_id_selectors)
        return payload


@dataclass(frozen=True)
class LaneTemplate:
    template_id: str
    base_grid_path: Path
    window_profiles: tuple[str, ...]
    model_families: tuple[str, ...] = ()
    stage2_feature_families: tuple[str, ...] = ()
    stage2_policy_families: tuple[str, ...] = ()
    stage3_policy_families: tuple[str, ...] = ()
    recipe_catalog_families: tuple[str, ...] = ()
    runtime_families: tuple[str, ...] = ()
    depends_on_templates: tuple[str, ...] = ()
    exclude_combinations: tuple[Dict[str, str], ...] = ()
    max_generated_lanes: int = 1
    resource: Optional[ResourceSpec] = None
    model_group: Optional[str] = None
    profile_id: Optional[str] = None
    model_bucket_url: Optional[str] = None

    def selected_values(self) -> Dict[str, tuple[str, ...]]:
        return {
            "window_profile": self.window_profiles,
            "model_family": self.model_families,
            "stage2_feature_family": self.stage2_feature_families,
            "stage2_policy_family": self.stage2_policy_families,
            "stage3_policy_family": self.stage3_policy_families,
            "recipe_catalog_family": self.recipe_catalog_families,
            "runtime_family": self.runtime_families,
        }

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "template_id": self.template_id,
            "base_grid_path": str(self.base_grid_path),
            "window_profiles": list(self.window_profiles),
            "depends_on_templates": list(self.depends_on_templates),
            "exclude_combinations": [dict(item) for item in self.exclude_combinations],
            "max_generated_lanes": int(self.max_generated_lanes),
        }
        for field_name in (
            "model_families",
            "stage2_feature_families",
            "stage2_policy_families",
            "stage3_policy_families",
            "recipe_catalog_families",
            "runtime_families",
        ):
            values = tuple(getattr(self, field_name))
            if values:
                payload[field_name] = list(values)
        if self.resource is not None:
            payload["resource"] = self.resource.to_dict()
        if self.model_group:
            payload["model_group"] = self.model_group
        if self.profile_id:
            payload["profile_id"] = self.profile_id
        if self.model_bucket_url:
            payload["model_bucket_url"] = self.model_bucket_url
        return payload


@dataclass(frozen=True)
class CampaignSpec:
    campaign_id: str
    manifest_path: Path
    inputs: CampaignInputs
    execution_defaults: ExecutionDefaults
    window_profiles: Dict[str, WindowProfile]
    families: Dict[str, Dict[str, FamilySpec]]
    lane_templates: tuple[LaneTemplate, ...]
    campaign_max_lanes: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "schema_version": 1,
            "experiment_kind": "factory_campaign_v1",
            "campaign_id": self.campaign_id,
            "manifest_path": str(self.manifest_path),
            "inputs": self.inputs.to_dict(),
            "execution_defaults": self.execution_defaults.to_dict(),
            "window_profiles": {
                name: profile.windows
                for name, profile in self.window_profiles.items()
            },
            "families": {
                family_group: {
                    name: family.to_dict()
                    for name, family in items.items()
                }
                for family_group, items in self.families.items()
            },
            "lane_templates": [template.to_dict() for template in self.lane_templates],
        }
        if self.campaign_max_lanes is not None:
            payload["campaign_max_lanes"] = int(self.campaign_max_lanes)
        return payload


def _normalize_name_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    names = tuple(str(item).strip() for item in value if str(item).strip())
    if value and not names:
        raise ValueError(f"{field} must not be empty when provided")
    if len(names) != len(set(names)):
        raise ValueError(f"{field} must not contain duplicates")
    return names


def _validate_window_profiles(payload: Dict[str, Any]) -> Dict[str, WindowProfile]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("window_profiles must be a non-empty object")
    profiles: Dict[str, WindowProfile] = {}
    for name, raw_profile in payload.items():
        profile_name = str(name).strip()
        if not profile_name:
            raise ValueError("window_profiles contains an empty name")
        if not isinstance(raw_profile, dict):
            raise ValueError(f"window_profiles.{profile_name} must be an object")
        windows: Dict[str, Dict[str, str]] = {}
        for key in _WINDOW_KEYS:
            raw_window = dict(raw_profile.get(key) or {})
            start = str(raw_window.get("start") or "").strip()
            end = str(raw_window.get("end") or "").strip()
            if not start or not end:
                raise ValueError(f"window_profiles.{profile_name}.{key} must define start and end")
            windows[key] = {"start": start, "end": end}
        profiles[profile_name] = WindowProfile(name=profile_name, windows=windows)
    return profiles


def _validate_family_payload(axis: str, target: str, payload: Dict[str, Any], *, field: str) -> None:
    if axis == "model_family":
        if target != "base_manifest":
            raise ValueError(f"{field}.target must be 'base_manifest'")
        models_by_stage = payload.get("models_by_stage")
        if not isinstance(models_by_stage, dict):
            raise ValueError(f"{field}.models_by_stage must be an object")
        for stage_name in ("stage1", "stage2", "stage3"):
            models = list((models_by_stage or {}).get(stage_name) or [])
            if not models:
                raise ValueError(f"{field}.models_by_stage.{stage_name} must not be empty")
        return

    if axis == "stage2_feature_family":
        feature_sets = list(payload.get("feature_sets") or [])
        if not feature_sets:
            raise ValueError(f"{field}.feature_sets must not be empty")
        return

    if axis == "stage2_policy_family":
        if not payload.get("stage2_policy_id") and not isinstance(payload.get("stage2"), dict):
            raise ValueError(f"{field} must define stage2_policy_id and/or stage2")
        return

    if axis == "stage3_policy_family":
        if not payload.get("stage3_policy_id") and not isinstance(payload.get("stage3"), dict):
            raise ValueError(f"{field} must define stage3_policy_id and/or stage3")
        return

    if axis == "recipe_catalog_family":
        if not str(payload.get("recipe_catalog_id") or "").strip():
            raise ValueError(f"{field}.recipe_catalog_id must be set")
        return

    if axis == "runtime_family":
        if "block_expiry" not in payload or not isinstance(payload.get("block_expiry"), bool):
            raise ValueError(f"{field}.block_expiry must be boolean")
        return

    raise ValueError(f"unsupported family axis: {axis}")


def _validate_families(payload: Dict[str, Any]) -> Dict[str, Dict[str, FamilySpec]]:
    if not isinstance(payload, dict):
        raise ValueError("families must be an object")
    families: Dict[str, Dict[str, FamilySpec]] = {key: {} for key in _FAMILY_GROUPS}
    for family_group, axis in _FAMILY_GROUPS.items():
        raw_group = payload.get(family_group) or {}
        if raw_group and not isinstance(raw_group, dict):
            raise ValueError(f"families.{family_group} must be an object")
        for name, raw_family in dict(raw_group).items():
            family_name = str(name).strip()
            if not family_name:
                raise ValueError(f"families.{family_group} contains an empty family name")
            if not isinstance(raw_family, dict):
                raise ValueError(f"families.{family_group}.{family_name} must be an object")
            target = str(raw_family.get("target") or "").strip()
            if target not in {"base_manifest", "grid_runs"}:
                raise ValueError(f"families.{family_group}.{family_name}.target must be one of ['base_manifest', 'grid_runs']")
            run_id_selectors = _normalize_name_list(
                raw_family.get("run_id_selectors"),
                field=f"families.{family_group}.{family_name}.run_id_selectors",
            )
            if target == "grid_runs" and not run_id_selectors:
                raise ValueError(f"families.{family_group}.{family_name}.run_id_selectors must be set for grid_runs families")
            family_payload = {key: value for key, value in raw_family.items() if key not in {"target", "run_id_selectors"}}
            _validate_family_payload(
                axis,
                target,
                family_payload,
                field=f"families.{family_group}.{family_name}",
            )
            families[family_group][family_name] = FamilySpec(
                axis=axis,
                name=family_name,
                target=target,  # type: ignore[arg-type]
                payload=family_payload,
                run_id_selectors=run_id_selectors,
            )
    return families


def _run_ids_from_grid_manifest(grid_path: Path) -> set[str]:
    resolved = load_and_resolve_manifest(grid_path, validate_paths=True)
    if str(resolved.get("experiment_kind") or "") != STAGED_GRID_KIND:
        raise ValueError(f"base_grid_path must resolve to experiment_kind={STAGED_GRID_KIND}")
    return {str(item.get("run_id") or "").strip() for item in list(((resolved.get("grid") or {}).get("runs") or []))}


def load_campaign_spec(path: Path) -> CampaignSpec:
    manifest_path = Path(path).resolve()
    payload = _read_json_object(manifest_path)
    manifest_dir = manifest_path.parent

    if str(payload.get("experiment_kind") or "factory_campaign_v1").strip() not in {"factory_campaign_v1"}:
        raise ValueError("experiment_kind must be 'factory_campaign_v1'")
    campaign_id = str(payload.get("campaign_id") or "").strip()
    if not campaign_id:
        raise ValueError("campaign_id must be set")

    inputs_payload = dict(payload.get("inputs") or {})
    parquet_root = _resolve_path(str(inputs_payload.get("parquet_root") or ""), manifest_dir=manifest_dir)
    support_dataset = str(inputs_payload.get("support_dataset") or "").strip()
    if not support_dataset:
        raise ValueError("inputs.support_dataset must be set")
    dataset_root = parquet_root / support_dataset
    if not dataset_root.exists():
        raise ValueError(f"campaign dataset root does not exist: {dataset_root}")

    execution_payload = dict(payload.get("execution_defaults") or {})
    poll_interval_seconds = float(execution_payload.get("poll_interval_seconds", 30.0))
    if poll_interval_seconds < 0:
        raise ValueError("execution_defaults.poll_interval_seconds must be >= 0")
    infra_max_attempts = int(execution_payload.get("infra_max_attempts", 2))
    if infra_max_attempts < 1:
        raise ValueError("execution_defaults.infra_max_attempts must be >= 1")
    total_cores = int(execution_payload.get("total_cores", max(1, int(os.cpu_count() or 1))))
    total_memory_gb = float(execution_payload.get("total_memory_gb", 1.0))
    if total_cores < 1:
        raise ValueError("execution_defaults.total_cores must be >= 1")
    if total_memory_gb <= 0:
        raise ValueError("execution_defaults.total_memory_gb must be > 0")
    ranking_strategy = str(execution_payload.get("ranking_strategy") or "publishable_economics_v1").strip()
    if not ranking_strategy:
        raise ValueError("execution_defaults.ranking_strategy must be set")
    model_group = str(execution_payload.get("model_group") or "").strip()
    profile_id = str(execution_payload.get("profile_id") or "").strip()
    if not model_group:
        raise ValueError("execution_defaults.model_group must be set")
    if not profile_id:
        raise ValueError("execution_defaults.profile_id must be set")
    model_bucket_url = str(execution_payload.get("model_bucket_url") or "").strip() or None

    window_profiles = _validate_window_profiles(dict(payload.get("window_profiles") or {}))
    families = _validate_families(dict(payload.get("families") or {}))

    raw_templates = list(payload.get("lane_templates") or [])
    if not raw_templates:
        raise ValueError("lane_templates must not be empty")

    seen_template_ids: set[str] = set()
    dependency_map: Dict[str, tuple[str, ...]] = {}
    lane_templates: list[LaneTemplate] = []
    for index, raw_template in enumerate(raw_templates, start=1):
        if not isinstance(raw_template, dict):
            raise ValueError(f"lane_templates[{index}] must be an object")
        template_id = str(raw_template.get("template_id") or "").strip()
        if not template_id:
            raise ValueError(f"lane_templates[{index}].template_id must be set")
        if template_id in seen_template_ids:
            raise ValueError(f"duplicate template_id: {template_id}")
        seen_template_ids.add(template_id)

        base_grid_value = str(raw_template.get("base_grid_path") or "").strip()
        if not base_grid_value:
            raise ValueError(f"lane_templates[{index}].base_grid_path must be set")
        base_grid_path = _resolve_path(base_grid_value, manifest_dir=manifest_dir)
        if not base_grid_path.exists():
            raise ValueError(f"lane_templates[{index}].base_grid_path does not exist: {base_grid_path}")
        run_ids = _run_ids_from_grid_manifest(base_grid_path)

        window_profile_names = _normalize_name_list(
            raw_template.get("window_profiles"),
            field=f"lane_templates[{index}].window_profiles",
        )
        if not window_profile_names:
            raise ValueError(f"lane_templates[{index}].window_profiles must not be empty")
        unknown_window_profiles = sorted(set(window_profile_names) - set(window_profiles))
        if unknown_window_profiles:
            raise ValueError(f"lane_templates[{index}] references unknown window_profiles: {unknown_window_profiles}")

        selected_families: Dict[str, tuple[str, ...]] = {}
        for family_group, axis in _FAMILY_GROUPS.items():
            values = _normalize_name_list(
                raw_template.get(family_group),
                field=f"lane_templates[{index}].{family_group}",
            )
            unknown = sorted(set(values) - set(families[family_group]))
            if unknown:
                raise ValueError(f"lane_templates[{index}] references unknown {family_group}: {unknown}")
            for family_name in values:
                family = families[family_group][family_name]
                if family.target == "grid_runs":
                    unknown_selectors = sorted(set(family.run_id_selectors) - run_ids)
                    if unknown_selectors:
                        raise ValueError(
                            f"lane_templates[{index}] uses family {family_group}.{family_name} with unknown run_id_selectors: {unknown_selectors}"
                        )
            selected_families[family_group] = values

        depends_on_templates = _normalize_name_list(
            raw_template.get("depends_on_templates"),
            field=f"lane_templates[{index}].depends_on_templates",
        )
        max_generated_lanes = int(raw_template.get("max_generated_lanes", 0))
        if max_generated_lanes < 1:
            raise ValueError(f"lane_templates[{index}].max_generated_lanes must be >= 1")
        exclude_combinations_raw = list(raw_template.get("exclude_combinations") or [])
        exclude_combinations: list[Dict[str, str]] = []
        for exclude_index, raw_exclusion in enumerate(exclude_combinations_raw, start=1):
            if not isinstance(raw_exclusion, dict) or not raw_exclusion:
                raise ValueError(f"lane_templates[{index}].exclude_combinations[{exclude_index}] must be a non-empty object")
            exclusion: Dict[str, str] = {}
            for axis_name, value in raw_exclusion.items():
                normalized_axis = str(axis_name).strip()
                normalized_value = str(value).strip()
                if normalized_axis not in _AXIS_TO_TEMPLATE_FIELD:
                    raise ValueError(
                        f"lane_templates[{index}].exclude_combinations[{exclude_index}] has unsupported axis: {normalized_axis}"
                    )
                if not normalized_value:
                    raise ValueError(
                        f"lane_templates[{index}].exclude_combinations[{exclude_index}].{normalized_axis} must be non-empty"
                    )
                exclusion[normalized_axis] = normalized_value
            exclude_combinations.append(exclusion)

        resource_payload = dict(raw_template.get("resource") or {})
        if not resource_payload:
            raise ValueError(f"lane_templates[{index}].resource must be set")
        cores = int(resource_payload.get("cores", 0))
        memory_gb = float(resource_payload.get("memory_gb", 0.0))
        if cores < 1:
            raise ValueError(f"lane_templates[{index}].resource.cores must be >= 1")
        if memory_gb <= 0:
            raise ValueError(f"lane_templates[{index}].resource.memory_gb must be > 0")

        dependency_map[template_id] = depends_on_templates
        lane_templates.append(
            LaneTemplate(
                template_id=template_id,
                base_grid_path=base_grid_path,
                window_profiles=window_profile_names,
                model_families=selected_families["model_families"],
                stage2_feature_families=selected_families["stage2_feature_families"],
                stage2_policy_families=selected_families["stage2_policy_families"],
                stage3_policy_families=selected_families["stage3_policy_families"],
                recipe_catalog_families=selected_families["recipe_catalog_families"],
                runtime_families=selected_families["runtime_families"],
                depends_on_templates=depends_on_templates,
                exclude_combinations=tuple(exclude_combinations),
                max_generated_lanes=max_generated_lanes,
                resource=ResourceSpec(cores=cores, memory_gb=memory_gb),
                model_group=(str(raw_template.get("model_group") or "").strip() or None),
                profile_id=(str(raw_template.get("profile_id") or "").strip() or None),
                model_bucket_url=(str(raw_template.get("model_bucket_url") or "").strip() or None),
            )
        )

    for template_id, depends_on in dependency_map.items():
        unknown = [item for item in depends_on if item not in dependency_map]
        if unknown:
            raise ValueError(f"lane template {template_id} depends on unknown templates: {unknown}")
    order_index = {template_id: idx for idx, template_id in enumerate(_topological_order(dependency_map))}
    lane_templates.sort(key=lambda template: (order_index[template.template_id], template.template_id))

    campaign_max_lanes = payload.get("campaign_max_lanes")
    if campaign_max_lanes is not None and int(campaign_max_lanes) < 1:
        raise ValueError("campaign_max_lanes must be >= 1 when provided")

    return CampaignSpec(
        campaign_id=campaign_id,
        manifest_path=manifest_path,
        inputs=CampaignInputs(parquet_root=parquet_root, support_dataset=support_dataset),
        execution_defaults=ExecutionDefaults(
            poll_interval_seconds=poll_interval_seconds,
            infra_max_attempts=infra_max_attempts,
            total_cores=total_cores,
            total_memory_gb=total_memory_gb,
            ranking_strategy=ranking_strategy,
            stop_on_first_publishable=bool(execution_payload.get("stop_on_first_publishable", False)),
            model_group=model_group,
            profile_id=profile_id,
            model_bucket_url=model_bucket_url,
        ),
        window_profiles=window_profiles,
        families=families,
        lane_templates=tuple(lane_templates),
        campaign_max_lanes=(None if campaign_max_lanes is None else int(campaign_max_lanes)),
    )


__all__ = [
    "CampaignInputs",
    "CampaignSpec",
    "ExecutionDefaults",
    "FamilySpec",
    "LaneTemplate",
    "WindowProfile",
    "load_campaign_spec",
]
