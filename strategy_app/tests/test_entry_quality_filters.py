from __future__ import annotations

from strategy_app.contracts import Direction, RiskContext
from strategy_app.engines.strategies.all_strategies import OIBuildupStrategy, ORBStrategy


def _base_payload(*, minutes: int) -> dict:
    return {
        "snapshot_id": f"snap-{minutes}",
        "session_context": {
            "snapshot_id": f"snap-{minutes}",
            "timestamp": "2026-04-01T10:00:00+05:30",
            "date": "2026-04-01",
            "session_phase": "ACTIVE",
            "minutes_since_open": minutes,
        },
        "chain_aggregates": {
            "atm_strike": 50000,
        },
        "atm_options": {
            "atm_ce_close": 100.0,
            "atm_pe_close": 100.0,
            "atm_ce_vol_ratio": 1.6,
            "atm_pe_vol_ratio": 1.6,
        },
    }


def test_orb_blocks_breakout_without_strong_confirmation() -> None:
    strategy = ORBStrategy()
    payload = _base_payload(minutes=45)
    payload["futures_bar"] = {"fut_close": 50110}
    payload["futures_derived"] = {"vol_ratio": 1.6, "fut_return_5m": 0.0010}
    payload["opening_range"] = {"orh": 50080, "orl": 49920, "orh_broken": True, "orl_broken": False}
    payload["chain_aggregates"]["pcr"] = 1.05

    vote = strategy.evaluate(payload, None, RiskContext())

    assert vote is None


def test_orb_accepts_high_quality_breakout() -> None:
    strategy = ORBStrategy()
    payload = _base_payload(minutes=45)
    payload["futures_bar"] = {"fut_close": 50120}
    payload["futures_derived"] = {"vol_ratio": 2.1, "fut_return_5m": 0.0014}
    payload["opening_range"] = {"orh": 50080, "orl": 49920, "orh_broken": True, "orl_broken": False}
    payload["chain_aggregates"]["pcr"] = 1.08

    vote = strategy.evaluate(payload, None, RiskContext())

    assert vote is not None
    assert vote.direction == Direction.CE


def test_oi_buildup_blocks_weak_low_liquidity_setup() -> None:
    strategy = OIBuildupStrategy()
    payload = _base_payload(minutes=60)
    payload["futures_bar"] = {"fut_oi": 100000}
    payload["futures_derived"] = {"fut_oi_change_30m": 2500, "fut_return_15m": 0.0012, "vol_ratio": 1.20}
    payload["chain_aggregates"]["pcr"] = 1.01

    vote = strategy.evaluate(payload, None, RiskContext())

    assert vote is None


def test_oi_buildup_accepts_strong_confirmed_setup() -> None:
    strategy = OIBuildupStrategy()
    payload = _base_payload(minutes=75)
    payload["futures_bar"] = {"fut_oi": 100000}
    payload["futures_derived"] = {"fut_oi_change_30m": 3500, "fut_return_15m": 0.0022, "vol_ratio": 1.55}
    payload["chain_aggregates"]["pcr"] = 1.08

    vote = strategy.evaluate(payload, None, RiskContext())

    assert vote is not None
    assert vote.direction == Direction.CE
