from __future__ import annotations

from strategy_app.engines.profiles import (
    PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1,
    PROFILE_TRADER_MASTER_V1,
    build_run_metadata,
    get_regime_entry_map,
    known_profile_ids,
)
from strategy_app.engines.strategy_router import StrategyRouter


def test_det_dir_profile_registered() -> None:
    assert PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1 in known_profile_ids()


def test_det_dir_has_ml_plus_rule_direction_strategies() -> None:
    mapping = get_regime_entry_map(PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1)
    assert mapping["TRENDING"][:2] == ["IV_FILTER", "ML_ENTRY"]
    assert "ORB" in mapping["TRENDING"]
    assert "OI_BUILDUP" in mapping["TRENDING"]
    ml_only = get_regime_entry_map(PROFILE_TRADER_MASTER_ML_ENTRY_V1)
    assert "ORB" not in ml_only["TRENDING"]
    master = get_regime_entry_map(PROFILE_TRADER_MASTER_V1)
    assert set(master["TRENDING"]) - {"IV_FILTER"} <= set(mapping["TRENDING"])


def test_det_dir_router_materializes() -> None:
    router = StrategyRouter()
    router.configure(build_run_metadata(PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1)["router_config"])
    names = {s.name for s in router.all_unique_strategies()}
    assert "ML_ENTRY" in names
    assert "ORB" in names
