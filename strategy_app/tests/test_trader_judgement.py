from __future__ import annotations

from strategy_app.contracts import Direction, RiskContext
from strategy_app.engines.strategies.all_strategies import TraderCompositeStrategy
from strategy_app.engines.trader_judgement import (
    OptionTradabilityScorer,
    TradeGovernor,
    TraderAction,
    TraderAnnotationRecord,
    TraderDayClassifier,
    TraderDayType,
    TraderSetupScorer,
    TraderSetupState,
    TraderSetupType,
)
from strategy_app.engines.snapshot_accessor import SnapshotAccessor


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
            "pcr": 1.04,
        },
        "atm_options": {
            "atm_ce_close": 120.0,
            "atm_pe_close": 118.0,
            "atm_ce_vol_ratio": 1.7,
            "atm_pe_vol_ratio": 1.7,
        },
        "futures_bar": {
            "fut_close": 50050.0,
            "fut_oi": 100000.0,
        },
        "futures_derived": {
            "vol_ratio": 1.4,
            "fut_return_5m": 0.0010,
            "fut_return_15m": 0.0012,
            "fut_return_30m": 0.0020,
            "fut_oi_change_30m": 1500.0,
            "vwap": 50000.0,
            "price_vs_vwap": 0.0010,
        },
        "opening_range": {
            "orh": 50080.0,
            "orl": 49920.0,
            "orh_broken": False,
            "orl_broken": False,
        },
        "iv_derived": {
            "iv_percentile": 60.0,
        },
        "vix_context": {
            "vix_spike_flag": False,
        },
    }


def test_trader_annotation_round_trip() -> None:
    record = TraderAnnotationRecord(
        snapshot_id="snap-1",
        trade_date="2026-04-01",
        day_type=TraderDayType.TREND.value,
        setup_type=TraderSetupType.ORB_RETEST.value,
        action=TraderAction.TAKE.value,
        direction=Direction.CE.value,
        option_plan="ATM",
        invalidation_reference="ORH",
        notes="clean retest hold",
    )

    payload = record.to_payload()
    restored = TraderAnnotationRecord.from_payload(payload)

    assert restored == record


def test_day_classifier_marks_trend_day() -> None:
    payload = _base_payload(minutes=65)
    payload["futures_bar"]["fut_close"] = 50120.0
    payload["futures_derived"]["vwap"] = 50010.0
    payload["futures_derived"]["fut_return_15m"] = 0.0015
    payload["futures_derived"]["fut_return_30m"] = 0.0025
    day = TraderDayClassifier().assess(SnapshotAccessor(payload))

    assert day.day_type == TraderDayType.TREND
    assert day.directional_bias == Direction.CE


def test_day_classifier_marks_no_trade_on_vix_spike() -> None:
    payload = _base_payload(minutes=70)
    payload["vix_context"]["vix_spike_flag"] = True
    day = TraderDayClassifier().assess(SnapshotAccessor(payload))

    assert day.day_type == TraderDayType.NO_TRADE


def test_day_classifier_marks_midday_low_energy_as_no_trade() -> None:
    payload = _base_payload(minutes=180)
    payload["futures_derived"]["vol_ratio"] = 0.9
    payload["futures_derived"]["fut_return_15m"] = 0.0002
    payload["futures_derived"]["price_vs_vwap"] = 0.0002
    day = TraderDayClassifier().assess(SnapshotAccessor(payload))

    assert day.day_type == TraderDayType.NO_TRADE


def test_setup_scorer_finds_orb_retest() -> None:
    scorer = TraderSetupScorer()
    state = TraderSetupState()

    breakout = _base_payload(minutes=40)
    breakout["futures_bar"]["fut_close"] = 50135.0
    breakout["futures_derived"]["fut_return_5m"] = 0.0012
    breakout["opening_range"]["orh_broken"] = True
    scorer.observe(SnapshotAccessor(breakout), state)

    retest = _base_payload(minutes=45)
    retest["futures_bar"]["fut_close"] = 50082.0
    retest["futures_derived"]["fut_return_5m"] = -0.0002
    retest["opening_range"]["orh_broken"] = True
    scorer.best_setup(
        SnapshotAccessor(retest),
        state,
        TraderDayClassifier().assess(SnapshotAccessor(retest)),
    )

    resume = _base_payload(minutes=50)
    resume["futures_bar"]["fut_close"] = 50122.0
    resume["futures_derived"]["fut_return_5m"] = 0.0010
    resume["futures_derived"]["fut_return_15m"] = 0.0014
    resume["opening_range"]["orh_broken"] = True
    setup = scorer.best_setup(
        SnapshotAccessor(resume),
        state,
        TraderDayClassifier().assess(SnapshotAccessor(resume)),
    )

    assert setup.trigger_ready is True
    assert setup.setup_type == TraderSetupType.ORB_RETEST
    assert setup.direction == Direction.CE


def test_option_tradability_blocks_expensive_option() -> None:
    payload = _base_payload(minutes=80)
    payload["atm_options"]["atm_ce_close"] = 450.0
    option = OptionTradabilityScorer().assess(SnapshotAccessor(payload), Direction.CE, expected_move_pct=0.003)

    assert option.tradable is False


def test_trade_governor_blocks_balanced_day_trades() -> None:
    governor = TradeGovernor()
    balanced = _base_payload(minutes=40)
    balanced["futures_derived"]["fut_return_15m"] = 0.0006
    balanced["futures_derived"]["fut_return_30m"] = 0.0008
    day = TraderDayClassifier().assess(SnapshotAccessor(balanced))
    scorer = TraderSetupScorer()
    state = TraderSetupState()

    breakout = _base_payload(minutes=40)
    breakout["futures_bar"]["fut_close"] = 50135.0
    breakout["futures_derived"]["fut_return_5m"] = 0.0012
    breakout["futures_derived"]["fut_return_15m"] = 0.0006
    breakout["futures_derived"]["fut_return_30m"] = 0.0008
    breakout["opening_range"]["orh_broken"] = True
    scorer.observe(SnapshotAccessor(breakout), state)

    retest = _base_payload(minutes=45)
    retest["futures_bar"]["fut_close"] = 50082.0
    retest["futures_derived"]["fut_return_5m"] = -0.0002
    retest["futures_derived"]["fut_return_15m"] = 0.0006
    retest["futures_derived"]["fut_return_30m"] = 0.0008
    retest["opening_range"]["orh_broken"] = True
    scorer.best_setup(SnapshotAccessor(retest), state, day)

    resume = _base_payload(minutes=50)
    resume["futures_bar"]["fut_close"] = 50122.0
    resume["futures_derived"]["fut_return_5m"] = 0.0010
    resume["futures_derived"]["fut_return_15m"] = 0.0006
    resume["futures_derived"]["fut_return_30m"] = 0.0008
    setup = scorer.best_setup(SnapshotAccessor(resume), state, day)
    option = OptionTradabilityScorer().assess(SnapshotAccessor(resume), Direction.CE, expected_move_pct=setup.expected_move_pct)
    decision = governor.evaluate(day=day, setup=setup, option=option, entries_taken=0)

    assert day.day_type == TraderDayType.BALANCED
    assert decision.allowed is False
    assert decision.reason == "balanced_day_skip"


def test_trader_composite_enters_on_retest_sequence() -> None:
    strategy = TraderCompositeStrategy()

    breakout = _base_payload(minutes=40)
    breakout["futures_bar"]["fut_close"] = 50135.0
    breakout["futures_derived"]["fut_return_5m"] = 0.0012
    breakout["opening_range"]["orh_broken"] = True
    assert strategy.evaluate(breakout, None, RiskContext()) is None

    retest = _base_payload(minutes=45)
    retest["futures_bar"]["fut_close"] = 50082.0
    retest["futures_derived"]["fut_return_5m"] = -0.0002
    retest["opening_range"]["orh_broken"] = True
    assert strategy.evaluate(retest, None, RiskContext()) is None

    resume = _base_payload(minutes=50)
    resume["futures_bar"]["fut_close"] = 50122.0
    resume["futures_derived"]["fut_return_5m"] = 0.0010
    resume["futures_derived"]["fut_return_15m"] = 0.0014
    resume["opening_range"]["orh_broken"] = True
    vote = strategy.evaluate(resume, None, RiskContext())

    assert vote is not None
    assert vote.direction == Direction.CE
    assert "annotation" in vote.raw_signals
    assert vote.raw_signals["setup_type"] == TraderSetupType.ORB_RETEST.value


def test_trader_composite_skips_no_trade_day() -> None:
    strategy = TraderCompositeStrategy()
    payload = _base_payload(minutes=75)
    payload["vix_context"]["vix_spike_flag"] = True

    vote = strategy.evaluate(payload, None, RiskContext())

    assert vote is not None
    assert vote.direction == Direction.AVOID


def test_trader_composite_skips_balanced_day_even_with_setup() -> None:
    strategy = TraderCompositeStrategy()

    breakout = _base_payload(minutes=40)
    breakout["futures_bar"]["fut_close"] = 50135.0
    breakout["futures_derived"]["fut_return_5m"] = 0.0012
    breakout["futures_derived"]["fut_return_15m"] = 0.0006
    breakout["futures_derived"]["fut_return_30m"] = 0.0008
    breakout["opening_range"]["orh_broken"] = True
    assert strategy.evaluate(breakout, None, RiskContext()) is None

    retest = _base_payload(minutes=45)
    retest["futures_bar"]["fut_close"] = 50082.0
    retest["futures_derived"]["fut_return_5m"] = -0.0002
    retest["futures_derived"]["fut_return_15m"] = 0.0006
    retest["futures_derived"]["fut_return_30m"] = 0.0008
    retest["opening_range"]["orh_broken"] = True
    assert strategy.evaluate(retest, None, RiskContext()) is None

    resume = _base_payload(minutes=50)
    resume["futures_bar"]["fut_close"] = 50122.0
    resume["futures_derived"]["fut_return_5m"] = 0.0010
    resume["futures_derived"]["fut_return_15m"] = 0.0006
    resume["futures_derived"]["fut_return_30m"] = 0.0008
    vote = strategy.evaluate(resume, None, RiskContext())

    assert vote is not None
    assert vote.direction == Direction.AVOID
    assert vote.reason == "TRADER_SKIP: balanced_day_skip"
