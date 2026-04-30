from __future__ import annotations

from strategy_app.contracts import Direction, RiskContext
from strategy_app.engines.strategies.all_strategies import (
    FailedBreakoutReversalStrategy,
    ORBRetestContinuationStrategy,
    VWAPPullbackContinuationStrategy,
)


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
            "pcr": 1.05,
        },
        "atm_options": {
            "atm_ce_close": 100.0,
            "atm_pe_close": 100.0,
            "atm_ce_vol_ratio": 1.8,
            "atm_pe_vol_ratio": 1.8,
        },
        "futures_bar": {
            "fut_close": 50050.0,
            "fut_oi": 100000.0,
        },
        "futures_derived": {
            "vol_ratio": 1.7,
            "fut_return_5m": 0.0010,
            "fut_return_15m": 0.0018,
            "fut_return_30m": 0.0028,
            "fut_oi_change_30m": 1800.0,
            "vwap": 50000.0,
            "price_vs_vwap": 0.0010,
        },
        "opening_range": {
            "orh": 50080.0,
            "orl": 49920.0,
            "orh_broken": False,
            "orl_broken": False,
        },
    }


def test_orb_retest_requires_retest_before_entry() -> None:
    strategy = ORBRetestContinuationStrategy()

    breakout = _base_payload(minutes=40)
    breakout["futures_bar"]["fut_close"] = 50130.0
    breakout["futures_derived"]["fut_return_5m"] = 0.0012
    breakout["opening_range"]["orh_broken"] = True
    assert strategy.evaluate(breakout, None, RiskContext()) is None

    retest = _base_payload(minutes=45)
    retest["futures_bar"]["fut_close"] = 50082.0
    retest["futures_derived"]["fut_return_5m"] = -0.0003
    retest["opening_range"]["orh_broken"] = True
    assert strategy.evaluate(retest, None, RiskContext()) is None

    resume = _base_payload(minutes=50)
    resume["futures_bar"]["fut_close"] = 50125.0
    resume["futures_derived"]["fut_return_5m"] = 0.0011
    resume["futures_derived"]["fut_return_15m"] = 0.0016
    resume["opening_range"]["orh_broken"] = True
    vote = strategy.evaluate(resume, None, RiskContext())

    assert vote is not None
    assert vote.direction == Direction.CE


def test_vwap_pullback_requires_bias_then_pullback_then_reacceptance() -> None:
    strategy = VWAPPullbackContinuationStrategy()

    bias = _base_payload(minutes=70)
    bias["futures_bar"]["fut_close"] = 50110.0
    bias["futures_derived"]["vwap"] = 50020.0
    bias["futures_derived"]["price_vs_vwap"] = 0.0018
    bias["futures_derived"]["fut_return_15m"] = 0.0018
    bias["futures_derived"]["fut_return_30m"] = 0.0030
    assert strategy.evaluate(bias, None, RiskContext()) is None

    pullback = _base_payload(minutes=78)
    pullback["futures_bar"]["fut_close"] = 50035.0
    pullback["futures_derived"]["vwap"] = 50020.0
    pullback["futures_derived"]["price_vs_vwap"] = 0.0003
    pullback["futures_derived"]["fut_return_5m"] = -0.0002
    pullback["futures_derived"]["fut_return_15m"] = 0.0015
    pullback["futures_derived"]["fut_return_30m"] = 0.0026
    assert strategy.evaluate(pullback, None, RiskContext()) is None

    reaccept = _base_payload(minutes=85)
    reaccept["futures_bar"]["fut_close"] = 50085.0
    reaccept["futures_derived"]["vwap"] = 50020.0
    reaccept["futures_derived"]["price_vs_vwap"] = 0.0013
    reaccept["futures_derived"]["fut_return_5m"] = 0.0010
    reaccept["futures_derived"]["fut_return_15m"] = 0.0017
    reaccept["futures_derived"]["fut_return_30m"] = 0.0027
    vote = strategy.evaluate(reaccept, None, RiskContext())

    assert vote is not None
    assert vote.direction == Direction.CE


def test_failed_breakout_reversal_requires_reentry() -> None:
    strategy = FailedBreakoutReversalStrategy()

    break_up = _base_payload(minutes=42)
    break_up["futures_bar"]["fut_close"] = 50140.0
    break_up["futures_derived"]["fut_return_5m"] = 0.0011
    break_up["opening_range"]["orh_broken"] = True
    assert strategy.evaluate(break_up, None, RiskContext()) is None

    fail_back = _base_payload(minutes=48)
    fail_back["futures_bar"]["fut_close"] = 50045.0
    fail_back["futures_derived"]["fut_return_5m"] = -0.0010
    fail_back["chain_aggregates"]["pcr"] = 0.95
    fail_back["opening_range"]["orh_broken"] = True
    vote = strategy.evaluate(fail_back, None, RiskContext())

    assert vote is not None
    assert vote.direction == Direction.PE
