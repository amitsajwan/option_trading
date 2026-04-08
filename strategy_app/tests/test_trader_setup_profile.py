from __future__ import annotations

from strategy_app.engines.profiles import (
    PROFILE_DET_SETUP_V1,
    build_run_metadata,
    get_exit_strategies,
    get_regime_entry_map,
    known_profile_ids,
)
from strategy_app.engines.strategy_router import StrategyRouter


def test_det_setup_v1_is_registered() -> None:
    assert PROFILE_DET_SETUP_V1 in known_profile_ids()


def test_det_setup_v1_routes_to_trader_style_setups() -> None:
    mapping = get_regime_entry_map(PROFILE_DET_SETUP_V1)
    assert mapping["TRENDING"] == ["IV_FILTER", "TRADER_COMPOSITE"]
    assert mapping["SIDEWAYS"] == ["IV_FILTER", "TRADER_COMPOSITE"]
    assert mapping["PRE_EXPIRY"] == ["IV_FILTER", "TRADER_COMPOSITE"]


def test_det_setup_v1_exit_strategies_are_setup_owned() -> None:
    exits = get_exit_strategies(PROFILE_DET_SETUP_V1)
    assert set(exits) == {"TRADER_COMPOSITE"}


def test_strategy_router_can_materialize_det_setup_v1() -> None:
    router = StrategyRouter()
    router.configure(build_run_metadata(PROFILE_DET_SETUP_V1)["router_config"])
    assert router.strategy_profile_id == PROFILE_DET_SETUP_V1
    summary = router.summary()
    assert summary["TRENDING"] == ["IV_FILTER", "TRADER_COMPOSITE"]
    assert summary["SIDEWAYS"] == ["IV_FILTER", "TRADER_COMPOSITE"]


def test_tournament_includes_det_setup_v1() -> None:
    from strategy_app.tools.deterministic_profile_tournament import default_profile_specs

    ids = {item.profile_id for item in default_profile_specs()}
    assert PROFILE_DET_SETUP_V1 in ids
