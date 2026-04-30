from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from strategy_app.engines.profiles import (
    PRODUCTION_DEFAULT_PROFILE_ID,
    PROFILE_DET_CORE_V2,
    PROFILE_DET_PROD_V1,
    build_run_metadata,
    get_exit_strategies,
    get_regime_entry_map,
    get_risk_config,
    known_profile_ids,
)


def test_production_default_is_det_prod_v1() -> None:
    assert PRODUCTION_DEFAULT_PROFILE_ID == "det_prod_v1"


def test_det_prod_v1_and_det_core_v2_are_registered() -> None:
    ids = known_profile_ids()
    assert "det_prod_v1" in ids
    assert "det_core_v2" in ids


def test_det_prod_v1_regime_entry_map() -> None:
    mapping = get_regime_entry_map(PROFILE_DET_PROD_V1)
    assert mapping["TRENDING"] == ["IV_FILTER", "ORB", "OI_BUILDUP"]
    assert mapping["SIDEWAYS"] == ["IV_FILTER", "OI_BUILDUP"]
    assert mapping["EXPIRY"] == ["IV_FILTER"]
    assert mapping["PRE_EXPIRY"] == ["IV_FILTER", "ORB", "OI_BUILDUP"]
    assert mapping["HIGH_VOL"] == ["IV_FILTER", "HIGH_VOL_ORB"]
    assert mapping["AVOID"] == []


def test_det_prod_v1_exit_strategies() -> None:
    exits = get_exit_strategies(PROFILE_DET_PROD_V1)
    assert set(exits) == {"ORB", "OI_BUILDUP", "HIGH_VOL_ORB"}


def test_det_prod_v1_risk_config() -> None:
    cfg = get_risk_config(PROFILE_DET_PROD_V1)
    assert cfg["stop_loss_pct"] == pytest.approx(0.20)
    assert cfg["target_pct"] == pytest.approx(0.80)
    assert cfg["trailing_enabled"] is True
    assert cfg["trailing_activation_pct"] == pytest.approx(0.10)
    assert cfg["trailing_offset_pct"] == pytest.approx(0.05)
    assert cfg["trailing_lock_breakeven"] is True


def test_det_core_v2_differs_from_det_prod_v1() -> None:
    prod_map = get_regime_entry_map(PROFILE_DET_PROD_V1)
    core_map = get_regime_entry_map(PROFILE_DET_CORE_V2)
    assert "VWAP_RECLAIM" in core_map["SIDEWAYS"]
    assert "VWAP_RECLAIM" not in prod_map["SIDEWAYS"]
    prod_exits = get_exit_strategies(PROFILE_DET_PROD_V1)
    core_exits = get_exit_strategies(PROFILE_DET_CORE_V2)
    assert "VWAP_RECLAIM" not in prod_exits
    assert "VWAP_RECLAIM" in core_exits


def test_det_core_v2_has_no_risk_override() -> None:
    assert get_risk_config(PROFILE_DET_CORE_V2) == {}


def test_strategy_router_default_profile_is_det_prod_v1() -> None:
    from strategy_app.engines.strategy_router import StrategyRouter

    router = StrategyRouter()
    assert router.strategy_profile_id == "det_prod_v1"


def test_deterministic_engine_default_profile_is_det_prod_v1() -> None:
    from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine

    engine = DeterministicRuleEngine(signal_logger=MagicMock())
    assert engine._strategy_profile_id == "det_prod_v1"


def test_engine_context_emits_det_prod_v1() -> None:
    from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine

    logger = MagicMock()
    engine = DeterministicRuleEngine(signal_logger=logger)
    engine.set_run_context("test-run", {"strategy_profile_id": "det_prod_v1"})
    assert engine._strategy_profile_id == "det_prod_v1"


def test_build_run_metadata_det_prod_v1() -> None:
    meta = build_run_metadata(PROFILE_DET_PROD_V1)
    assert meta["strategy_profile_id"] == "det_prod_v1"
    assert meta["router_config"]["regime_entry_map"]["TRENDING"] == ["IV_FILTER", "ORB", "OI_BUILDUP"]
    assert set(meta["router_config"]["exit_strategies"]) == {"ORB", "OI_BUILDUP", "HIGH_VOL_ORB"}
    assert meta["risk_config"]["stop_loss_pct"] == pytest.approx(0.20)
    assert meta["risk_config"]["trailing_enabled"] is True


def test_tournament_profiles_include_det_prod_v1_and_det_core_v2() -> None:
    from strategy_app.tools.deterministic_profile_tournament import default_profile_specs

    ids = {item.profile_id for item in default_profile_specs()}
    assert "det_prod_v1" in ids
    assert "det_core_v2" in ids


def test_tournament_det_prod_v1_sources_from_registry() -> None:
    from strategy_app.tools.deterministic_profile_tournament import default_profile_specs

    prod = next(item for item in default_profile_specs() if item.profile_id == "det_prod_v1")
    router_config = prod.metadata.get("router_config", {})
    assert router_config.get("regime_entry_map", {}).get("TRENDING") == ["IV_FILTER", "ORB", "OI_BUILDUP"]
    assert set(router_config.get("exit_strategies", [])) == {"ORB", "OI_BUILDUP", "HIGH_VOL_ORB"}


def test_sensitivity_variants_source_from_registry() -> None:
    from strategy_app.tools.deterministic_risk_sensitivity import default_winner_variants

    expected_map = get_regime_entry_map(PROFILE_DET_PROD_V1)
    for item in default_winner_variants():
        router_config = item.metadata.get("router_config", {})
        assert router_config.get("regime_entry_map") == expected_map
