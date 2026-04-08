"""Shared deterministic strategy profile definitions."""

from __future__ import annotations

from typing import Any

PROFILE_DET_PROD_V1 = "det_prod_v1"
PROFILE_DET_CORE_V2 = "det_core_v2"
PROFILE_DET_SETUP_V1 = "det_setup_v1"

PRODUCTION_DEFAULT_PROFILE_ID = PROFILE_DET_PROD_V1

_DET_PROD_V1_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": ["IV_FILTER", "ORB", "OI_BUILDUP"],
    "SIDEWAYS": ["IV_FILTER", "OI_BUILDUP"],
    "EXPIRY": ["IV_FILTER"],
    "PRE_EXPIRY": ["IV_FILTER", "ORB", "OI_BUILDUP"],
    "HIGH_VOL": ["IV_FILTER", "HIGH_VOL_ORB"],
    "AVOID": [],
}

_DET_PROD_V1_EXIT_STRATEGIES: list[str] = ["ORB", "OI_BUILDUP", "HIGH_VOL_ORB"]

_DET_PROD_V1_RISK_CONFIG: dict[str, Any] = {
    "stop_loss_pct": 0.20,
    "target_pct": 0.80,
    "trailing_enabled": True,
    "trailing_activation_pct": 0.10,
    "trailing_offset_pct": 0.05,
    "trailing_lock_breakeven": True,
}

_DET_CORE_V2_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": ["IV_FILTER", "ORB", "OI_BUILDUP", "PREV_DAY_LEVEL"],
    "SIDEWAYS": ["IV_FILTER", "VWAP_RECLAIM", "OI_BUILDUP"],
    "EXPIRY": ["IV_FILTER", "VWAP_RECLAIM"],
    "PRE_EXPIRY": ["IV_FILTER", "ORB", "OI_BUILDUP"],
    "HIGH_VOL": ["IV_FILTER", "HIGH_VOL_ORB"],
    "AVOID": [],
}

_DET_CORE_V2_EXIT_STRATEGIES: list[str] = ["ORB", "VWAP_RECLAIM", "OI_BUILDUP"]

_DET_SETUP_V1_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": ["IV_FILTER", "TRADER_COMPOSITE"],
    "SIDEWAYS": ["IV_FILTER", "TRADER_COMPOSITE"],
    "EXPIRY": ["IV_FILTER"],
    "PRE_EXPIRY": ["IV_FILTER", "TRADER_COMPOSITE"],
    "HIGH_VOL": ["IV_FILTER", "TRADER_COMPOSITE"],
    "AVOID": [],
}

_DET_SETUP_V1_EXIT_STRATEGIES: list[str] = ["TRADER_COMPOSITE"]

_DET_SETUP_V1_RISK_CONFIG: dict[str, Any] = {
    "stop_loss_pct": 0.20,
    "target_pct": 0.80,
    "trailing_enabled": True,
    "trailing_activation_pct": 0.10,
    "trailing_offset_pct": 0.05,
    "trailing_lock_breakeven": True,
}

_PROFILE_REGIME_ENTRY_MAPS: dict[str, dict[str, list[str]]] = {
    PROFILE_DET_PROD_V1: _DET_PROD_V1_REGIME_ENTRY_MAP,
    PROFILE_DET_CORE_V2: _DET_CORE_V2_REGIME_ENTRY_MAP,
    PROFILE_DET_SETUP_V1: _DET_SETUP_V1_REGIME_ENTRY_MAP,
}

_PROFILE_EXIT_STRATEGIES: dict[str, list[str]] = {
    PROFILE_DET_PROD_V1: _DET_PROD_V1_EXIT_STRATEGIES,
    PROFILE_DET_CORE_V2: _DET_CORE_V2_EXIT_STRATEGIES,
    PROFILE_DET_SETUP_V1: _DET_SETUP_V1_EXIT_STRATEGIES,
}

_PROFILE_RISK_CONFIGS: dict[str, dict[str, Any]] = {
    PROFILE_DET_PROD_V1: _DET_PROD_V1_RISK_CONFIG,
    PROFILE_DET_SETUP_V1: _DET_SETUP_V1_RISK_CONFIG,
}


def known_profile_ids() -> list[str]:
    return list(_PROFILE_REGIME_ENTRY_MAPS.keys())


def get_regime_entry_map(profile_id: str) -> dict[str, list[str]]:
    return {str(key): list(value) for key, value in _PROFILE_REGIME_ENTRY_MAPS[str(profile_id)].items()}


def get_exit_strategies(profile_id: str) -> list[str]:
    return list(_PROFILE_EXIT_STRATEGIES[str(profile_id)])


def get_risk_config(profile_id: str) -> dict[str, Any]:
    return dict(_PROFILE_RISK_CONFIGS.get(str(profile_id), {}))


def build_router_config(profile_id: str) -> dict[str, Any]:
    return {
        "strategy_profile_id": str(profile_id),
        "regime_entry_map": get_regime_entry_map(profile_id),
        "exit_strategies": get_exit_strategies(profile_id),
    }


def build_run_metadata(profile_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "strategy_profile_id": str(profile_id),
        "router_config": build_router_config(profile_id),
    }
    risk_config = get_risk_config(profile_id)
    if risk_config:
        payload["risk_config"] = risk_config
    return payload


__all__ = [
    "PRODUCTION_DEFAULT_PROFILE_ID",
    "PROFILE_DET_CORE_V2",
    "PROFILE_DET_PROD_V1",
    "PROFILE_DET_SETUP_V1",
    "build_router_config",
    "build_run_metadata",
    "get_exit_strategies",
    "get_regime_entry_map",
    "get_risk_config",
    "known_profile_ids",
]
