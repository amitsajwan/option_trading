from __future__ import annotations

from strategy_app.engines.profiles import (
    PROFILE_TRADER_MASTER_V1,
    build_run_metadata,
    get_regime_entry_map,
    get_exit_strategies,
    get_risk_config,
    known_profile_ids,
)
from strategy_app.engines.strategy_router import StrategyRouter


def test_trader_master_profile_registered() -> None:
    assert PROFILE_TRADER_MASTER_V1 in known_profile_ids()


def test_trader_master_regime_union() -> None:
    mapping = get_regime_entry_map(PROFILE_TRADER_MASTER_V1)
    trending = mapping["TRENDING"]
    sideways = mapping["SIDEWAYS"]
    assert "ORB" in trending
    assert "R2_TOP3_LONG_CE" in trending
    assert "R1S_TOP3_SHORT_CE" in trending
    assert "TRADER_V3_COMPOSITE" in trending
    assert "VWAP_RECLAIM" in sideways
    assert "R1_TOP3_LONG_PE" in sideways
    assert "R2_TOP3_LONG_CE" not in sideways
    assert mapping["AVOID"] == []


def test_trader_master_router_materializes_all_entries() -> None:
    router = StrategyRouter()
    router.configure(build_run_metadata(PROFILE_TRADER_MASTER_V1)["router_config"])
    assert router.strategy_profile_id == PROFILE_TRADER_MASTER_V1
    names = {s.name for s in router.all_unique_strategies()}
    assert "ORB" in names
    assert "R1_TOP3_LONG_PE" in names
    assert "PBV1_TOP3_THESIS" in names


def test_trader_master_exit_helpers_separate_from_trending_entries() -> None:
    mapping = get_regime_entry_map(PROFILE_TRADER_MASTER_V1)
    exits = get_exit_strategies(PROFILE_TRADER_MASTER_V1)
    assert "ORB" in exits
    assert "ORB" in mapping["TRENDING"]
    risk = get_risk_config(PROFILE_TRADER_MASTER_V1)
    assert risk["trailing_enabled"] is True
    assert risk["stop_loss_pct"] == 0.25
