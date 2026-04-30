from __future__ import annotations

from datetime import datetime

from contracts_app import IST_ZONE
from strategy_app.contracts import Direction, RiskContext, SignalType, StrategyVote
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.engines.entry_policy import EntryPolicyDecision
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.trader_v3 import StrikeSelectorV3, TradeGovernorV3, TraderV3CompositeStrategy, TraderV3Playbook
from strategy_app.engines.trader_regime_v3 import TraderRegimeV3, TraderRegimeV3Label


def _payload(
    *,
    minutes: int,
    trade_date: str = "2026-04-01",
    expiry: bool = False,
    close: float = 50090.0,
    max_pain: int = 50000,
) -> dict:
    return {
        "snapshot_id": f"snap-{trade_date}-{minutes}",
        "session_context": {
            "snapshot_id": f"snap-{trade_date}-{minutes}",
            "timestamp": f"{trade_date}T10:00:00+05:30",
            "date": trade_date,
            "session_phase": "ACTIVE",
            "minutes_since_open": minutes,
            "days_to_expiry": 0 if expiry else 2,
            "is_expiry_day": expiry,
        },
        "chain_aggregates": {"atm_strike": 50000, "max_pain": max_pain},
        "futures_bar": {"fut_close": close},
        "futures_derived": {
            "fut_return_5m": 0.0010,
            "fut_return_15m": 0.0014,
            "fut_return_30m": 0.0021,
            "realized_vol_30m": 0.012,
            "vol_ratio": 1.5,
            "vwap": 50020.0,
            "price_vs_vwap": 0.0014,
        },
        "opening_range": {"orh": 50040.0, "orl": 49940.0, "orh_broken": True, "orl_broken": False},
        "atm_options": {
            "atm_ce_close": 118.0,
            "atm_pe_close": 108.0,
            "atm_ce_vol_ratio": 1.7,
            "atm_pe_vol_ratio": 1.6,
            "atm_ce_iv": 0.18,
            "atm_pe_iv": 0.19,
        },
        "iv_derived": {"iv_percentile": 60.0, "iv_skew": -0.02},
        "vix_context": {"vix_spike_flag": False},
        "strikes": [
            {"strike": 49900, "ce_ltp": 150.0, "ce_delta": 0.62, "ce_oi": 25000.0, "ce_volume": 21000.0, "pe_ltp": 78.0, "pe_delta": -0.38, "pe_oi": 18000.0, "pe_volume": 17500.0},
            {"strike": 50000, "ce_ltp": 118.0, "ce_delta": 0.52, "ce_oi": 27000.0, "ce_volume": 23000.0, "pe_ltp": 108.0, "pe_delta": -0.48, "pe_oi": 26000.0, "pe_volume": 22500.0},
            {"strike": 50100, "ce_ltp": 94.0, "ce_delta": 0.42, "ce_oi": 30000.0, "ce_volume": 26000.0, "pe_ltp": 132.0, "pe_delta": -0.58, "pe_oi": 29000.0, "pe_volume": 24000.0},
        ],
    }


class _BlockingPolicy:
    def evaluate(self, snap, vote, regime, risk):
        return EntryPolicyDecision.block("forced_block", {"mode": "test"})


def test_strike_selector_v3_selects_delta_band_contract() -> None:
    snap = SnapshotAccessor(_payload(minutes=55))
    from strategy_app.engines.options_state import OptionsStateBuilder

    options_state = OptionsStateBuilder().build(snap)
    selection = StrikeSelectorV3().select(
        options_state=options_state,
        direction=Direction.CE,
        playbook=TraderV3Playbook.TREND_PULLBACK_LONG,
        expected_move_pct=0.0025,
    )

    assert selection is not None
    assert selection.target_delta_band == "0.45-0.65"
    assert selection.strike in {49900, 50000}


def test_trade_governor_v3_blocks_overtrading() -> None:
    governor = TradeGovernorV3()
    decision = governor.evaluate(
        snap=SnapshotAccessor(_payload(minutes=60)),
        regime=TraderRegimeV3(TraderRegimeV3Label.TREND_UP, Direction.CE, 0.84, ("trend",)),
        playbook_signal=type("PlaybookSignalObj", (), {"playbook": TraderV3Playbook.TREND_PULLBACK_LONG, "score": 0.84, "expected_move_pct": 0.0025})(),
        strike_selection=type("StrikeSelectionObj", (), {"option_score": 0.70})(),
        state=type("StateObj", (), {"bad_session_lockout": False, "entries_taken": 2, "playbook_entries": {}})(),
    )

    assert decision.allowed is False
    assert decision.reason == "session_entry_cap"


def test_trade_governor_v3_blocks_disabled_expiry_momentum() -> None:
    governor = TradeGovernorV3()
    decision = governor.evaluate(
        snap=SnapshotAccessor(_payload(minutes=45, expiry=True)),
        regime=TraderRegimeV3(TraderRegimeV3Label.EXPIRY_MOMENTUM, Direction.CE, 0.88, ("expiry",)),
        playbook_signal=type("PlaybookSignalObj", (), {"playbook": TraderV3Playbook.EXPIRY_MOMENTUM_BREAK, "score": 0.90, "expected_move_pct": 0.0030})(),
        strike_selection=type("StrikeSelectionObj", (), {"option_score": 0.80})(),
        state=type("StateObj", (), {"bad_session_lockout": False, "entries_taken": 0, "playbook_entries": {}})(),
    )

    assert decision.allowed is False
    assert decision.reason == "expiry_momentum_disabled"


def test_trade_governor_v3_blocks_pre_expiry_trend_pullback() -> None:
    governor = TradeGovernorV3()
    payload = _payload(minutes=60)
    payload["session_context"]["days_to_expiry"] = 1
    decision = governor.evaluate(
        snap=SnapshotAccessor(payload),
        regime=TraderRegimeV3(TraderRegimeV3Label.TREND_UP, Direction.CE, 0.84, ("trend",)),
        playbook_signal=type("PlaybookSignalObj", (), {"playbook": TraderV3Playbook.TREND_PULLBACK_LONG, "score": 0.86, "expected_move_pct": 0.0024})(),
        strike_selection=type("StrikeSelectionObj", (), {"option_score": 0.70})(),
        state=type("StateObj", (), {"bad_session_lockout": False, "entries_taken": 0, "playbook_entries": {}})(),
    )

    assert decision.allowed is False
    assert decision.reason == "pre_expiry_trend_pullback_disabled"


def test_trader_v3_composite_enters_on_trend_pullback() -> None:
    strategy = TraderV3CompositeStrategy()

    first = _payload(minutes=35)
    assert strategy.evaluate(first, None, RiskContext()) is None

    second = _payload(minutes=40)
    second["futures_bar"]["fut_close"] = 50025.0
    second["futures_derived"]["fut_return_5m"] = -0.0002
    second["futures_derived"]["price_vs_vwap"] = 0.0001
    assert strategy.evaluate(second, None, RiskContext()) is None

    third = _payload(minutes=45)
    vote = strategy.evaluate(third, None, RiskContext())

    assert vote is not None
    assert vote.signal_type == SignalType.ENTRY
    assert vote.direction == Direction.CE
    assert vote.raw_signals["playbook"] == TraderV3Playbook.TREND_PULLBACK_LONG.value
    assert vote.raw_signals["_lock_strike_selection"] is True
    assert vote.raw_signals["_entry_policy_mode"] == "bypass"


def test_trader_v3_composite_enters_failed_breakout_reversal() -> None:
    strategy = TraderV3CompositeStrategy()

    breakout = _payload(minutes=35)
    breakout["futures_bar"]["fut_close"] = 50120.0
    breakout["futures_derived"]["fut_return_5m"] = 0.0012
    assert strategy.evaluate(breakout, None, RiskContext()) is None

    reverse = _payload(minutes=40)
    reverse["futures_bar"]["fut_close"] = 50020.0
    reverse["futures_derived"]["fut_return_5m"] = -0.0008
    reverse["futures_derived"]["fut_return_15m"] = -0.0009
    reverse["futures_derived"]["fut_return_30m"] = -0.0008
    reverse["futures_derived"]["price_vs_vwap"] = 0.0004
    vote = strategy.evaluate(reverse, None, RiskContext())

    assert vote is not None
    assert vote.signal_type == SignalType.ENTRY
    assert vote.direction == Direction.PE
    assert vote.raw_signals["playbook"] == TraderV3Playbook.FAILED_BREAKOUT_REVERSAL_SHORT.value


def test_trader_v3_composite_skips_midday_dead_session() -> None:
    strategy = TraderV3CompositeStrategy()
    payload = _payload(minutes=180)
    payload["futures_derived"]["fut_return_15m"] = 0.0002
    payload["futures_derived"]["fut_return_30m"] = 0.0003
    payload["futures_derived"]["vol_ratio"] = 0.9
    payload["futures_derived"]["price_vs_vwap"] = 0.0001

    vote = strategy.evaluate(payload, None, RiskContext())

    assert vote is not None
    assert vote.signal_type == SignalType.SKIP
    assert vote.raw_signals["trader_regime_v3"] == TraderRegimeV3Label.NO_TRADE.value


def test_trader_v3_composite_uses_expiry_pin_playbook() -> None:
    strategy = TraderV3CompositeStrategy()
    payload = _payload(minutes=180, expiry=True, close=50040.0, max_pain=50000)
    payload["futures_derived"]["fut_return_5m"] = -0.0004
    payload["futures_derived"]["fut_return_15m"] = 0.0002
    payload["futures_derived"]["vol_ratio"] = 1.0
    payload["futures_derived"]["price_vs_vwap"] = 0.0008

    vote = strategy.evaluate(payload, None, RiskContext())

    assert vote is not None
    assert vote.signal_type == SignalType.ENTRY
    assert vote.raw_signals["trader_regime_v3"] == TraderRegimeV3Label.EXPIRY_PINNING.value
    assert vote.raw_signals["playbook"] == TraderV3Playbook.EXPIRY_PIN_REVERSAL.value


def test_trader_v3_composite_skips_disabled_expiry_momentum() -> None:
    strategy = TraderV3CompositeStrategy()
    payload = _payload(minutes=25, expiry=True, close=50120.0)
    payload["futures_derived"]["fut_return_5m"] = 0.0015
    payload["futures_derived"]["fut_return_15m"] = 0.0022
    payload["futures_derived"]["fut_return_30m"] = 0.0030
    payload["futures_derived"]["realized_vol_30m"] = 0.022
    payload["futures_derived"]["price_vs_vwap"] = 0.0020
    payload["opening_range"]["orh_broken"] = True
    payload["opening_range"]["orl_broken"] = False

    vote = strategy.evaluate(payload, None, RiskContext())

    assert vote is not None
    assert vote.signal_type == SignalType.SKIP
    assert vote.raw_signals["governor_reason"] == "expiry_momentum_disabled"


def test_engine_does_not_override_locked_v3_strike() -> None:
    engine = DeterministicRuleEngine(min_confidence=0.65)
    vote = StrategyVote(
        strategy_name="TRADER_V3_COMPOSITE",
        snapshot_id="snap-lock",
        timestamp=datetime(2026, 4, 1, 10, 0, tzinfo=IST_ZONE),
        trade_date="2026-04-01",
        signal_type=SignalType.ENTRY,
        direction=Direction.CE,
        confidence=0.85,
        reason="test",
        raw_signals={"_lock_strike_selection": True},
        proposed_strike=50100,
        proposed_entry_premium=94.0,
    )

    engine._apply_strike_selection(vote, SnapshotAccessor(_payload(minutes=60)))

    assert vote.proposed_strike == 50100
    assert vote.proposed_entry_premium == 94.0


def test_engine_bypasses_entry_policy_for_v3_votes() -> None:
    engine = DeterministicRuleEngine(entry_policy=_BlockingPolicy())
    snap = SnapshotAccessor(_payload(minutes=60))
    from strategy_app.engines.regime import Regime, RegimeSignal

    vote = StrategyVote(
        strategy_name="TRADER_V3_COMPOSITE",
        snapshot_id="snap-bypass",
        timestamp=datetime(2026, 4, 1, 10, 0, tzinfo=IST_ZONE),
        trade_date="2026-04-01",
        signal_type=SignalType.ENTRY,
        direction=Direction.CE,
        confidence=0.85,
        reason="TRADER_V3_ENTRY: playbook=TREND_PULLBACK_LONG",
        raw_signals={"_entry_policy_mode": "bypass", "_lock_strike_selection": True},
        proposed_strike=50000,
        proposed_entry_premium=118.0,
    )
    regime_signal = RegimeSignal(regime=Regime.TRENDING, confidence=0.90, reason="test", evidence={})

    signal = engine._process_entry_votes([vote], snap, engine._risk.context, regime_signal)

    assert signal is not None
    assert signal.entry_strategy_name == "TRADER_V3_COMPOSITE"
