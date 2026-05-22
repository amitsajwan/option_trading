from __future__ import annotations

from strategy_app.engines.profiles import (
    PROFILE_TRADER_MASTER_ML_ENTRY_V1,
    PROFILE_TRADER_MASTER_V1,
    build_run_metadata,
    get_exit_strategies,
    get_regime_entry_map,
    get_risk_config,
    known_profile_ids,
)
from strategy_app.engines.strategy_router import StrategyRouter


def test_ml_entry_profile_registered() -> None:
    assert PROFILE_TRADER_MASTER_ML_ENTRY_V1 in known_profile_ids()


def test_ml_entry_uses_iv_filter_and_ml_only_not_rule_strategies() -> None:
    mapping = get_regime_entry_map(PROFILE_TRADER_MASTER_ML_ENTRY_V1)
    assert mapping["TRENDING"] == ["IV_FILTER", "ML_ENTRY"]
    assert "ORB" not in mapping["TRENDING"]
    assert "PBV1_TOP3_THESIS" not in mapping["TRENDING"]
    master = get_regime_entry_map(PROFILE_TRADER_MASTER_V1)
    assert len(master["TRENDING"]) > 2


def test_ml_entry_shares_trader_master_exits_and_risk() -> None:
    exits = get_exit_strategies(PROFILE_TRADER_MASTER_ML_ENTRY_V1)
    master_exits = get_exit_strategies(PROFILE_TRADER_MASTER_V1)
    assert exits == master_exits
    assert "ORB" in exits
    assert get_risk_config(PROFILE_TRADER_MASTER_ML_ENTRY_V1) == get_risk_config(PROFILE_TRADER_MASTER_V1)


def test_ml_entry_router_materializes() -> None:
    router = StrategyRouter()
    router.configure(build_run_metadata(PROFILE_TRADER_MASTER_ML_ENTRY_V1)["router_config"])
    assert router.strategy_profile_id == PROFILE_TRADER_MASTER_ML_ENTRY_V1
    names = {s.name for s in router.all_unique_strategies()}
    assert "ML_ENTRY" in names
    assert "ORB" in names  # exit universe
