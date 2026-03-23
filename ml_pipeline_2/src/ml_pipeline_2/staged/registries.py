from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Callable, Dict


@dataclass(frozen=True)
class ViewSpec:
    view_id: str
    dataset_name: str
    stage: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@lru_cache(maxsize=None)
def view_registry() -> Dict[str, ViewSpec]:
    return {
        "stage1_entry_view_v1": ViewSpec("stage1_entry_view_v1", "stage1_entry_view", "stage1"),
        "stage2_direction_view_v1": ViewSpec("stage2_direction_view_v1", "stage2_direction_view", "stage2"),
        "stage3_recipe_view_v1": ViewSpec("stage3_recipe_view_v1", "stage3_recipe_view", "stage3"),
    }


@lru_cache(maxsize=None)
def label_registry() -> dict[str, str]:
    return {
        "entry_best_recipe_v1": "stage1",
        "direction_best_recipe_v1": "stage2",
        "recipe_best_positive_v1": "stage3",
    }


@lru_cache(maxsize=None)
def trainer_registry() -> dict[str, str]:
    return {
        "binary_catalog_v1": "binary_catalog",
        "ovr_recipe_catalog_v1": "ovr_recipe_catalog",
    }


@lru_cache(maxsize=None)
def policy_registry() -> dict[str, str]:
    return {
        "entry_threshold_v1": "stage1",
        "direction_dual_threshold_v1": "stage2",
        "recipe_top_margin_v1": "stage3",
    }


@lru_cache(maxsize=None)
def publish_registry() -> dict[str, str]:
    return {
        "staged_bundle_v1": "staged_bundle",
    }


def resolve_labeler(labeler_id: str) -> Callable[..., Any]:
    from .pipeline import build_stage1_labels, build_stage2_labels, build_stage3_labels

    registry = {
        "entry_best_recipe_v1": build_stage1_labels,
        "direction_best_recipe_v1": build_stage2_labels,
        "recipe_best_positive_v1": build_stage3_labels,
    }
    if labeler_id not in registry:
        raise ValueError(f"unknown labeler_id: {labeler_id}; valid options: {sorted(registry)}")
    return registry[labeler_id]


def resolve_trainer(trainer_id: str) -> Callable[..., Any]:
    from .pipeline import train_binary_catalog_stage, train_recipe_ovr_stage

    registry = {
        "binary_catalog_v1": train_binary_catalog_stage,
        "ovr_recipe_catalog_v1": train_recipe_ovr_stage,
    }
    if trainer_id not in registry:
        raise ValueError(f"unknown trainer_id: {trainer_id}; valid options: {sorted(registry)}")
    return registry[trainer_id]


def resolve_policy(policy_id: str) -> Callable[..., Any]:
    from .pipeline import select_direction_policy, select_entry_policy, select_recipe_policy

    registry = {
        "entry_threshold_v1": select_entry_policy,
        "direction_dual_threshold_v1": select_direction_policy,
        "recipe_top_margin_v1": select_recipe_policy,
    }
    if policy_id not in registry:
        raise ValueError(f"unknown policy_id: {policy_id}; valid options: {sorted(registry)}")
    return registry[policy_id]


def resolve_publisher(publisher_id: str) -> Callable[..., Any]:
    from .publish import publish_staged_run

    registry = {
        "staged_bundle_v1": publish_staged_run,
    }
    if publisher_id not in registry:
        raise ValueError(f"unknown publisher_id: {publisher_id}; valid options: {sorted(registry)}")
    return registry[publisher_id]
