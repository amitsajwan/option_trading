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
        "stage1_entry_view_v2": ViewSpec("stage1_entry_view_v2", "stage1_entry_view_v2", "stage1"),
        "stage2_direction_view_v2": ViewSpec("stage2_direction_view_v2", "stage2_direction_view_v2", "stage2"),
        "stage3_recipe_view_v2": ViewSpec("stage3_recipe_view_v2", "stage3_recipe_view_v2", "stage3"),
        "stage1_entry_view_v3": ViewSpec("stage1_entry_view_v3", "stage1_entry_view_v3", "stage1"),
        "stage2_direction_view_v3": ViewSpec("stage2_direction_view_v3", "stage2_direction_view_v3", "stage2"),
        "stage3_recipe_view_v3": ViewSpec("stage3_recipe_view_v3", "stage3_recipe_view_v3", "stage3"),
        "stage1_entry_view_v3_candidate": ViewSpec(
            "stage1_entry_view_v3_candidate",
            "stage1_entry_view_v3_candidate",
            "stage1",
        ),
        "stage2_direction_view_v3_candidate": ViewSpec(
            "stage2_direction_view_v3_candidate",
            "stage2_direction_view_v3_candidate",
            "stage2",
        ),
        "stage3_recipe_view_v3_candidate": ViewSpec(
            "stage3_recipe_view_v3_candidate",
            "stage3_recipe_view_v3_candidate",
            "stage3",
        ),
        # Dhan monthly-regime views — built from snapshots_dhan_v1 via
        # rebuild_stage_views_from_flat (output_stage*_dataset=*_dhan). Same v2
        # projection; separate datasets so the v2 views are untouched.
        "stage1_entry_view_dhan": ViewSpec("stage1_entry_view_dhan", "stage1_entry_view_dhan", "stage1"),
        "stage2_direction_view_dhan": ViewSpec("stage2_direction_view_dhan", "stage2_direction_view_dhan", "stage2"),
        "stage3_recipe_view_dhan": ViewSpec("stage3_recipe_view_dhan", "stage3_recipe_view_dhan", "stage3"),
    }


@lru_cache(maxsize=None)
def label_registry() -> dict[str, str]:
    return {
        "entry_best_recipe_v1": "stage1",
        "entry_bn_5m_100pts_v1": "stage1",
        "entry_bn_5m_up_v1": "stage1",
        "entry_bn_5m_down_v1": "stage1",
        "entry_bn_clean_move_strict_v1": "stage1",
        "entry_bn_clean_move_soft_v1": "stage1",
        "direction_best_recipe_v1": "stage2",
        "direction_or_no_trade_v1": "stage2",
        "direction_market_up_v1": "stage2",
        "direction_market_up_all_v1": "stage2",
        "ce_win_v1": "stage2",
        "pe_win_v1": "stage2",
        "recipe_best_positive_v1": "stage3",
    }


@lru_cache(maxsize=None)
def trainer_registry() -> dict[str, str]:
    return {
        "binary_catalog_v1": "binary_catalog",
        "gate_direction_catalog_v1": "gate_direction_catalog",
        "ovr_recipe_catalog_v1": "ovr_recipe_catalog",
    }


@lru_cache(maxsize=None)
def policy_registry() -> dict[str, str]:
    return {
        "entry_threshold_v1": "stage1",
        "direction_dual_threshold_v1": "stage2",
        "direction_gate_threshold_v1": "stage2",
        "direction_gate_economic_balance_v1": "stage2",
        "recipe_top_margin_v1": "stage3",
        "recipe_economic_balance_v1": "stage3",
        "recipe_fixed_baseline_guard_v1": "stage3",
    }


@lru_cache(maxsize=None)
def publish_registry() -> dict[str, str]:
    return {
        "staged_bundle_v1": "staged_bundle",
    }


def resolve_labeler(labeler_id: str) -> Callable[..., Any]:
    from .pipeline import (
        build_stage1_labels,
        build_stage1_labels_entry_bn_clean_move,
        build_stage1_labels_entry_bn_move,
        build_stage2_labels,
        build_stage2_labels_ce_win_v1,
        build_stage2_labels_direction_or_no_trade,
        build_stage2_labels_market_direction,
        build_stage2_labels_market_direction_all_rows,
        build_stage2_labels_pe_win_v1,
        build_stage3_labels,
    )

    registry = {
        "entry_best_recipe_v1": build_stage1_labels,
        "entry_bn_5m_100pts_v1": build_stage1_labels_entry_bn_move,
        "entry_bn_5m_up_v1": build_stage1_labels_entry_bn_move,
        "entry_bn_5m_down_v1": build_stage1_labels_entry_bn_move,
        "entry_bn_clean_move_strict_v1": build_stage1_labels_entry_bn_clean_move,
        "entry_bn_clean_move_soft_v1": build_stage1_labels_entry_bn_clean_move,
        "direction_best_recipe_v1": build_stage2_labels,
        "direction_or_no_trade_v1": build_stage2_labels_direction_or_no_trade,
        "direction_market_up_v1": build_stage2_labels_market_direction,
        "direction_market_up_all_v1": build_stage2_labels_market_direction_all_rows,
        "ce_win_v1": build_stage2_labels_ce_win_v1,
        "pe_win_v1": build_stage2_labels_pe_win_v1,
        "recipe_best_positive_v1": build_stage3_labels,
    }
    if labeler_id not in registry:
        raise ValueError(f"unknown labeler_id: {labeler_id}; valid options: {sorted(registry)}")
    return registry[labeler_id]


def resolve_trainer(trainer_id: str) -> Callable[..., Any]:
    from .pipeline import train_binary_catalog_stage, train_gate_direction_stage, train_recipe_ovr_stage

    registry = {
        "binary_catalog_v1": train_binary_catalog_stage,
        "gate_direction_catalog_v1": train_gate_direction_stage,
        "ovr_recipe_catalog_v1": train_recipe_ovr_stage,
    }
    if trainer_id not in registry:
        raise ValueError(f"unknown trainer_id: {trainer_id}; valid options: {sorted(registry)}")
    return registry[trainer_id]


def resolve_policy(policy_id: str) -> Callable[..., Any]:
    from .pipeline import (
        select_direction_gate_economic_balance_policy,
        select_direction_gate_policy,
        select_direction_policy,
        select_entry_policy,
        select_recipe_economic_balance_policy,
        select_recipe_fixed_baseline_guard_policy,
        select_recipe_policy,
    )

    registry = {
        "entry_threshold_v1": select_entry_policy,
        "direction_dual_threshold_v1": select_direction_policy,
        "direction_gate_threshold_v1": select_direction_gate_policy,
        "direction_gate_economic_balance_v1": select_direction_gate_economic_balance_policy,
        "recipe_top_margin_v1": select_recipe_policy,
        "recipe_economic_balance_v1": select_recipe_economic_balance_policy,
        "recipe_fixed_baseline_guard_v1": select_recipe_fixed_baseline_guard_policy,
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
