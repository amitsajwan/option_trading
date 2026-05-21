from __future__ import annotations

from strategy_app.engines.profiles import (
    PROFILE_R1S_TOP3_PAPER_V1,
    build_run_metadata,
    get_regime_entry_map,
    get_risk_config,
)
from strategy_app.engines.strategy_router import StrategyRouter


def test_r1s_top3_paper_profile_routes_only_r1s_strategy() -> None:
    mapping = get_regime_entry_map(PROFILE_R1S_TOP3_PAPER_V1)
    assert mapping["TRENDING"] == ["R1S_TOP3_SHORT_CE"]
    assert mapping["EXPIRY"] == []
    risk = get_risk_config(PROFILE_R1S_TOP3_PAPER_V1)
    assert risk["stop_loss_pct"] == 1.0
    assert risk["target_pct"] == 0.5
    assert risk["trailing_enabled"] is False


def test_router_loads_r1s_top3_profile() -> None:
    router = StrategyRouter()
    router.configure(build_run_metadata(PROFILE_R1S_TOP3_PAPER_V1)["router_config"])
    assert router.strategy_profile_id == PROFILE_R1S_TOP3_PAPER_V1
    names = {s.name for s in router.all_unique_strategies()}
    assert "R1S_TOP3_SHORT_CE" in names
