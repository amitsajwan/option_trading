from __future__ import annotations

from datetime import date

from strategy_app.contracts import Direction, SignalType
from strategy_app.engines.profiles import (
    PROFILE_DEBIT_MULTI_V1,
    build_run_metadata,
    get_regime_entry_map,
    get_risk_config,
)
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.strategies.rule_top3_long_option import (
    R1Top3LongPeStrategy,
    R2Top3LongCeStrategy,
)
from strategy_app.engines.strategy_router import StrategyRouter


def test_debit_multi_profile_splits_ce_and_pe_by_regime() -> None:
    mapping = get_regime_entry_map(PROFILE_DEBIT_MULTI_V1)
    assert "R2_TOP3_LONG_CE" in mapping["TRENDING"]
    assert "R1_TOP3_LONG_PE" in mapping["SIDEWAYS"]
    assert "R2_TOP3_LONG_CE" not in mapping["SIDEWAYS"]
    assert "R1_TOP3_LONG_PE" not in mapping["TRENDING"]
    risk = get_risk_config(PROFILE_DEBIT_MULTI_V1)
    assert risk["stop_loss_pct"] == 0.30
    assert risk["trailing_enabled"] is False


def test_router_loads_debit_multi_strategies() -> None:
    router = StrategyRouter()
    router.configure(build_run_metadata(PROFILE_DEBIT_MULTI_V1)["router_config"])
    assert router.strategy_profile_id == PROFILE_DEBIT_MULTI_V1
    names = {s.name for s in router.all_unique_strategies()}
    assert "R1_TOP3_LONG_PE" in names
    assert "R2_TOP3_LONG_CE" in names


def _orb_down_payload(*, minute: int) -> dict:
    return {
        "snapshot_id": f"snap-{minute}",
        "session_context": {
            "snapshot_id": f"snap-{minute}",
            "timestamp": "2024-05-15T10:00:00+05:30",
            "date": "2024-05-15",
            "minutes_since_open": minute,
            "is_expiry_day": False,
        },
        "futures_derived": {"fut_return_5m": -0.002, "price_vs_vwap": -0.001},
        "ctx_opening_range_ready": 1.0,
        "ctx_opening_range_breakout_down": 1.0,
        "chain_aggregates": {"atm_strike": 50000},
        "atm_options": {"atm_pe_close": 95.0},
    }


def _orb_up_payload(*, minute: int) -> dict:
    return {
        "snapshot_id": f"snap-{minute}",
        "session_context": {
            "snapshot_id": f"snap-{minute}",
            "timestamp": "2024-05-15T10:00:00+05:30",
            "date": "2024-05-15",
            "minutes_since_open": minute,
            "is_expiry_day": False,
        },
        "futures_derived": {"fut_return_5m": 0.002, "price_vs_vwap": 0.001},
        "ctx_opening_range_ready": 1.0,
        "ctx_opening_range_breakout_up": 1.0,
        "chain_aggregates": {"atm_strike": 50000},
        "atm_options": {"atm_ce_close": 120.0},
    }


def test_r1_long_pe_emits_debit_entry() -> None:
    strategy = R1Top3LongPeStrategy()
    strategy.on_session_start(date(2024, 5, 15))
    vote = strategy.evaluate(_orb_down_payload(minute=30), None, None)
    assert vote is not None
    assert vote.direction == Direction.PE
    assert vote.raw_signals.get("_debit_long_option") is True


def test_r2_long_ce_emits_debit_entry() -> None:
    strategy = R2Top3LongCeStrategy()
    strategy.on_session_start(date(2024, 5, 15))
    vote = strategy.evaluate(_orb_up_payload(minute=30), None, None)
    assert vote is not None
    assert vote.direction == Direction.CE
    assert vote.raw_signals.get("_debit_long_option") is True
