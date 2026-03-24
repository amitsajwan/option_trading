from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "LiveMarketSnapshotBuilder",
    "MARKET_SNAPSHOT_CONTRACT_ID",
    "MarketSnapshotState",
    "SNAPSHOT_ML_FLAT_SCHEMA_VERSION",
    "build_market_snapshot",
    "load_contract_schema",
    "load_legacy_mapping",
    "project_stage1_entry_view",
    "project_stage2_direction_view",
    "project_stage3_recipe_view",
    "project_stage_views",
    "validate_market_snapshot",
    "validate_snapshot_ml_flat_frame",
    "validate_snapshot_ml_flat_rows",
]


def __getattr__(name: str) -> Any:
    if name in {"LiveMarketSnapshotBuilder", "MarketSnapshotState", "build_market_snapshot"}:
        module = import_module(".market_snapshot", __name__)
        return getattr(module, name)
    if name in {"MARKET_SNAPSHOT_CONTRACT_ID", "validate_market_snapshot"}:
        module = import_module(".market_snapshot_contract", __name__)
        return getattr(module, name)
    if name in {
        "SNAPSHOT_ML_FLAT_SCHEMA_VERSION",
        "load_contract_schema",
        "load_legacy_mapping",
        "validate_snapshot_ml_flat_frame",
        "validate_snapshot_ml_flat_rows",
    }:
        module = import_module(".snapshot_ml_flat_contract", __name__)
        return getattr(module, name)
    if name in {
        "project_stage1_entry_view",
        "project_stage2_direction_view",
        "project_stage3_recipe_view",
        "project_stage_views",
    }:
        module = import_module(".stage_views", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
