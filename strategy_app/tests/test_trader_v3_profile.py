from __future__ import annotations

from strategy_app.engines.profiles import (
    PROFILE_DET_V3_V1,
    build_run_metadata,
    get_exit_strategies,
    get_regime_entry_map,
    known_profile_ids,
)
from strategy_app.engines.strategy_router import StrategyRouter


def test_det_v3_v1_is_registered() -> None:
    assert PROFILE_DET_V3_V1 in known_profile_ids()


def test_det_v3_v1_routes_to_trader_v3_composite() -> None:
    mapping = get_regime_entry_map(PROFILE_DET_V3_V1)
    assert mapping["TRENDING"] == ["IV_FILTER", "TRADER_V3_COMPOSITE"]
    assert mapping["EXPIRY"] == ["IV_FILTER", "TRADER_V3_COMPOSITE"]
    assert mapping["HIGH_VOL"] == ["IV_FILTER", "TRADER_V3_COMPOSITE"]


def test_det_v3_v1_exit_strategies_are_setup_owned() -> None:
    exits = get_exit_strategies(PROFILE_DET_V3_V1)
    assert exits == ["TRADER_V3_COMPOSITE"]


def test_strategy_router_can_materialize_det_v3_v1() -> None:
    router = StrategyRouter()
    router.configure(build_run_metadata(PROFILE_DET_V3_V1)["router_config"])
    summary = router.summary()
    assert summary["TRENDING"] == ["IV_FILTER", "TRADER_V3_COMPOSITE"]
    assert summary["EXPIRY"] == ["IV_FILTER", "TRADER_V3_COMPOSITE"]
