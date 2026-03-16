import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from contracts_app import IST_ZONE
from strategy_app.contracts import Direction, ExitReason, PositionContext, RiskContext, SignalType, StrategyVote, TradeSignal
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.engines.entry_policy import EntryPolicyDecision, PolicyConfig
from strategy_app.engines.ml_entry_policy import MLEntryPolicy
from strategy_app.engines.regime import Regime, RegimeSignal
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.strategy_router import StrategyRouter
from strategy_app.logging.signal_logger import SignalLogger
from strategy_app.position.tracker import PositionTracker


def _snapshot(
    *,
    snapshot_id: str,
    ts: str,
    ce_price: float,
    pe_price: float = 100.0,
    atm_strike: int = 50000,
    strikes: list[dict[str, float]] | None = None,
) -> SnapshotAccessor:
    strike_rows = strikes or [
        {
            "strike": float(atm_strike),
            "ce_ltp": float(ce_price),
            "pe_ltp": float(pe_price),
        }
    ]
    return SnapshotAccessor(
        {
            "snapshot_id": snapshot_id,
            "session_context": {
                "snapshot_id": snapshot_id,
                "timestamp": ts,
                "date": ts[:10],
                "session_phase": "ACTIVE",
                "days_to_expiry": 1,
            },
            "chain_aggregates": {
                "atm_strike": atm_strike,
                "strike_count": len(strike_rows),
            },
            "atm_options": {
                "atm_ce_close": ce_price,
                "atm_pe_close": pe_price,
            },
            "strikes": strike_rows,
        }
    )


class PositionRiskTests(unittest.TestCase):
    class _FakeEntryPolicy:
        def __init__(self, blocked: set[str] | None = None) -> None:
            self.blocked = blocked or set()

        def evaluate(self, snap: SnapshotAccessor, vote: StrategyVote, regime: RegimeSignal, risk: RiskContext) -> EntryPolicyDecision:
            del snap, regime, risk
            if vote.strategy_name in self.blocked:
                return EntryPolicyDecision.block("blocked", {"policy": "BLOCK"})
            return EntryPolicyDecision.allow("allowed", score=0.90, checks={"policy": "PASS"})

    class _ConflictResolvingEntryPolicy:
        def __init__(self, *, ce_score: float, pe_score: float) -> None:
            self._scores = {"CE": float(ce_score), "PE": float(pe_score)}

        def can_resolve_direction_conflict(self) -> bool:
            return True

        def evaluate(self, snap: SnapshotAccessor, vote: StrategyVote, regime: RegimeSignal, risk: RiskContext) -> EntryPolicyDecision:
            del snap, regime, risk
            key = vote.direction.value if vote.direction in (Direction.CE, Direction.PE) else ""
            if key not in self._scores:
                return EntryPolicyDecision.block("unsupported_direction", {"policy": "BLOCK:direction"})
            return EntryPolicyDecision.allow(
                f"allowed score={self._scores[key]:.2f}",
                score=self._scores[key],
                checks={"policy": "PASS"},
            )

    def test_fixed_stop_loss_exit(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-1",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        exit_signal = tracker.update(
            _snapshot(snapshot_id="snap-stop", ts="2026-02-28T09:35:00+05:30", ce_price=89.0),
            RiskContext(),
        )

        self.assertIsNotNone(exit_signal)
        self.assertEqual(exit_signal.exit_reason, ExitReason.STOP_LOSS)

    def test_position_without_max_hold_bars_keeps_running(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-no-max-hold",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
            max_hold_bars=None,
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        out_1 = tracker.update(_snapshot(snapshot_id="snap-1", ts="2026-02-28T09:31:00+05:30", ce_price=100.0), RiskContext())
        out_2 = tracker.update(_snapshot(snapshot_id="snap-2", ts="2026-02-28T09:32:00+05:30", ce_price=100.0), RiskContext())
        out_3 = tracker.update(_snapshot(snapshot_id="snap-3", ts="2026-02-28T09:33:00+05:30", ce_price=100.0), RiskContext())

        self.assertIsNone(out_1)
        self.assertIsNone(out_2)
        self.assertIsNone(out_3)
        self.assertIsNotNone(tracker.current_position)
        self.assertEqual(tracker.current_position.bars_held, 3)

    def test_marks_position_using_held_strike_not_shifted_atm(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-strike-hold",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="PE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
        )
        tracker.open_position(
            signal,
            _snapshot(
                snapshot_id="snap-open",
                ts=ts.isoformat(),
                ce_price=80.0,
                pe_price=100.0,
                atm_strike=50000,
                strikes=[
                    {"strike": 50000.0, "ce_ltp": 80.0, "pe_ltp": 100.0},
                    {"strike": 50100.0, "ce_ltp": 70.0, "pe_ltp": 300.0},
                ],
            ),
        )

        hold_signal = tracker.update(
            _snapshot(
                snapshot_id="snap-shift",
                ts="2026-02-28T09:31:00+05:30",
                ce_price=70.0,
                pe_price=300.0,
                atm_strike=50100,
                strikes=[
                    {"strike": 50000.0, "ce_ltp": 78.0, "pe_ltp": 95.0},
                    {"strike": 50100.0, "ce_ltp": 70.0, "pe_ltp": 300.0},
                ],
            ),
            RiskContext(),
        )

        self.assertIsNone(hold_signal)
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertAlmostEqual(float(position.current_premium), 95.0, places=6)
        self.assertAlmostEqual(float(position.pnl_pct), -0.05, places=6)

    def test_missing_held_strike_quote_fails_closed_without_atm_substitution(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-missing-strike",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="PE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
        )
        tracker.open_position(
            signal,
            _snapshot(
                snapshot_id="snap-open",
                ts=ts.isoformat(),
                ce_price=80.0,
                pe_price=100.0,
                atm_strike=50000,
                strikes=[{"strike": 50000.0, "ce_ltp": 80.0, "pe_ltp": 100.0}],
            ),
        )

        hold_signal = tracker.update(
            _snapshot(
                snapshot_id="snap-missing",
                ts="2026-02-28T09:31:00+05:30",
                ce_price=70.0,
                pe_price=250.0,
                atm_strike=50100,
                strikes=[{"strike": 50100.0, "ce_ltp": 70.0, "pe_ltp": 250.0}],
            ),
            RiskContext(),
        )

        self.assertIsNone(hold_signal)
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertEqual(position.bars_held, 1)
        self.assertAlmostEqual(float(position.current_premium), 100.0, places=6)

    def test_trailing_stop_exit(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-2",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
            trailing_enabled=True,
            trailing_activation_pct=0.10,
            trailing_offset_pct=0.05,
            trailing_lock_breakeven=True,
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        hold_signal = tracker.update(
            _snapshot(snapshot_id="snap-up", ts="2026-02-28T09:35:00+05:30", ce_price=120.0),
            RiskContext(),
        )

        self.assertIsNone(hold_signal)
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertTrue(position.trailing_active)
        self.assertAlmostEqual(position.stop_price or 0.0, 111.6, places=6)

        exit_signal = tracker.update(
            _snapshot(snapshot_id="snap-trail", ts="2026-02-28T09:40:00+05:30", ce_price=111.0),
            RiskContext(),
        )

        self.assertIsNotNone(exit_signal)
        self.assertEqual(exit_signal.exit_reason, ExitReason.TRAILING_STOP)

    def test_tiered_trailing_stop_offsets(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-tier-trail",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=1.00,
            trailing_enabled=True,
            trailing_lock_breakeven=True,
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        tracker.update(_snapshot(snapshot_id="snap-tier-1", ts="2026-02-28T09:31:00+05:30", ce_price=118.0), RiskContext())
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertTrue(position.trailing_active)
        self.assertAlmostEqual(position.stop_price or 0.0, 109.74, places=6)

        tracker.update(_snapshot(snapshot_id="snap-tier-2", ts="2026-02-28T09:32:00+05:30", ce_price=130.0), RiskContext())
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.stop_price or 0.0, 123.5, places=6)

        tracker.update(_snapshot(snapshot_id="snap-tier-3", ts="2026-02-28T09:33:00+05:30", ce_price=150.0), RiskContext())
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.stop_price or 0.0, 145.5, places=6)

    def test_orb_premium_trailing_stop_exit(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-orb-trail",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
            entry_strategy_name="ORB",
            orb_trail_activation_mfe=0.15,
            orb_trail_offset_pct=0.08,
            orb_trail_min_lock_pct=0.05,
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        hold_signal = tracker.update(
            _snapshot(snapshot_id="snap-up", ts="2026-02-28T09:35:00+05:30", ce_price=130.0),
            RiskContext(),
        )

        self.assertIsNone(hold_signal)
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertTrue(position.orb_trail_active)
        self.assertAlmostEqual(position.orb_trail_stop_price or 0.0, 119.6, places=6)
        self.assertAlmostEqual(position.stop_price or 0.0, 119.6, places=6)

        exit_signal = tracker.update(
            _snapshot(snapshot_id="snap-orb-trail", ts="2026-02-28T09:40:00+05:30", ce_price=119.0),
            RiskContext(),
        )

        self.assertIsNotNone(exit_signal)
        self.assertEqual(exit_signal.exit_reason, ExitReason.TRAILING_STOP)

    def test_orb_premium_trail_respects_min_lock_floor(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-orb-lock",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
            entry_strategy_name="ORB",
            orb_trail_activation_mfe=0.15,
            orb_trail_offset_pct=0.20,
            orb_trail_min_lock_pct=0.05,
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        tracker.update(
            _snapshot(snapshot_id="snap-up", ts="2026-02-28T09:35:00+05:30", ce_price=116.0),
            RiskContext(),
        )

        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertTrue(position.orb_trail_active)
        self.assertAlmostEqual(position.orb_trail_stop_price or 0.0, 105.0, places=6)
        self.assertAlmostEqual(position.stop_price or 0.0, 105.0, places=6)

        exit_signal = tracker.update(
            _snapshot(snapshot_id="snap-lock-stop", ts="2026-02-28T09:40:00+05:30", ce_price=104.0),
            RiskContext(),
        )

        self.assertIsNotNone(exit_signal)
        self.assertEqual(exit_signal.exit_reason, ExitReason.TRAILING_STOP)

    def test_orb_premium_trail_respects_regime_filter(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-orb-filter",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
            entry_strategy_name="ORB",
            entry_regime_name="TRENDING",
            orb_trail_activation_mfe=0.15,
            orb_trail_offset_pct=0.08,
            orb_trail_min_lock_pct=0.05,
            orb_trail_regime_filter="PRE_EXPIRY",
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        hold_signal = tracker.update(
            _snapshot(snapshot_id="snap-up", ts="2026-02-28T09:35:00+05:30", ce_price=130.0),
            RiskContext(),
        )

        self.assertIsNone(hold_signal)
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertFalse(position.orb_trail_active)
        self.assertIsNone(position.orb_trail_stop_price)

    def test_oi_premium_trailing_stop_exit_in_pre_expiry(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-oi-trail",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
            entry_strategy_name="OI_BUILDUP",
            entry_regime_name="PRE_EXPIRY",
            oi_trail_activation_mfe=0.15,
            oi_trail_offset_pct=0.08,
            oi_trail_min_lock_pct=0.05,
            oi_trail_regime_filter="PRE_EXPIRY",
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        hold_signal = tracker.update(
            _snapshot(snapshot_id="snap-up", ts="2026-02-28T09:35:00+05:30", ce_price=130.0),
            RiskContext(),
        )

        self.assertIsNone(hold_signal)
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertTrue(position.oi_trail_active)
        self.assertAlmostEqual(position.oi_trail_stop_price or 0.0, 119.6, places=6)
        self.assertAlmostEqual(position.stop_price or 0.0, 119.6, places=6)

        exit_signal = tracker.update(
            _snapshot(snapshot_id="snap-oi-trail", ts="2026-02-28T09:40:00+05:30", ce_price=119.0),
            RiskContext(),
        )

        self.assertIsNotNone(exit_signal)
        self.assertEqual(exit_signal.exit_reason, ExitReason.TRAILING_STOP)

    def test_oi_premium_trail_respects_regime_filter(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-oi-filter",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
            entry_strategy_name="OI_BUILDUP",
            entry_regime_name="TRENDING",
            oi_trail_activation_mfe=0.15,
            oi_trail_offset_pct=0.08,
            oi_trail_min_lock_pct=0.05,
            oi_trail_regime_filter="PRE_EXPIRY",
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        hold_signal = tracker.update(
            _snapshot(snapshot_id="snap-up", ts="2026-02-28T09:35:00+05:30", ce_price=130.0),
            RiskContext(),
        )

        self.assertIsNone(hold_signal)
        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertFalse(position.oi_trail_active)
        self.assertIsNone(position.oi_trail_stop_price)

    def test_oi_premium_trail_respects_min_lock_floor(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=IST_ZONE)
        signal = TradeSignal(
            signal_id="sig-oi-lock",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            stop_loss_pct=0.10,
            target_pct=0.50,
            entry_strategy_name="OI_BUILDUP",
            entry_regime_name="PRE_EXPIRY",
            oi_trail_activation_mfe=0.15,
            oi_trail_offset_pct=0.20,
            oi_trail_min_lock_pct=0.05,
            oi_trail_regime_filter="PRE_EXPIRY",
        )
        tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        tracker.update(
            _snapshot(snapshot_id="snap-up", ts="2026-02-28T09:35:00+05:30", ce_price=116.0),
            RiskContext(),
        )

        position = tracker.current_position
        self.assertIsNotNone(position)
        self.assertTrue(position.oi_trail_active)
        self.assertAlmostEqual(position.oi_trail_stop_price or 0.0, 105.0, places=6)
        self.assertAlmostEqual(position.stop_price or 0.0, 105.0, places=6)

        exit_signal = tracker.update(
            _snapshot(snapshot_id="snap-lock-stop", ts="2026-02-28T09:40:00+05:30", ce_price=104.0),
            RiskContext(),
        )

        self.assertIsNotNone(exit_signal)
        self.assertEqual(exit_signal.exit_reason, ExitReason.TRAILING_STOP)

    def test_engine_run_risk_override_applies_to_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prior = os.environ.get("STRATEGY_REDIS_PUBLISH_ENABLED")
            os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = "0"
            try:
                engine = DeterministicRuleEngine(signal_logger=SignalLogger(Path(tmpdir)))
                engine.on_session_start(date(2026, 2, 28))
                engine.set_run_context(
                    "run-1",
                    {
                        "risk_config": {
                            "stop_loss_pct": 0.12,
                            "target_pct": 0.30,
                            "trailing_enabled": True,
                            "trailing_activation_pct": 0.15,
                            "trailing_offset_pct": 0.07,
                            "trailing_lock_breakeven": False,
                        }
                    }
                )
                vote = StrategyVote(
                    strategy_name="TEST",
                    snapshot_id="snap-1",
                    timestamp=datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc),
                    trade_date="2026-02-28",
                    signal_type=SignalType.ENTRY,
                    direction=Direction.CE,
                    confidence=0.80,
                    reason="test vote",
                    proposed_strike=50000,
                    proposed_entry_premium=100.0,
                )
                snap = _snapshot(snapshot_id="snap-1", ts="2026-02-28T09:30:00+00:00", ce_price=100.0)
                regime_signal = RegimeSignal(regime=Regime.TRENDING, confidence=0.90, reason="test", evidence={})

                signal = engine._build_entry_signal(vote, snap, engine._risk.context, [vote], regime_signal)

                self.assertIsNotNone(signal)
                self.assertAlmostEqual(signal.stop_loss_pct, 0.12, places=6)
                self.assertAlmostEqual(signal.target_pct, 0.30, places=6)
                self.assertTrue(signal.trailing_enabled)
                self.assertAlmostEqual(signal.trailing_activation_pct, 0.15, places=6)
                self.assertAlmostEqual(signal.trailing_offset_pct, 0.07, places=6)
                self.assertFalse(signal.trailing_lock_breakeven)
            finally:
                if prior is None:
                    os.environ.pop("STRATEGY_REDIS_PUBLISH_ENABLED", None)
                else:
                    os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = prior

    def test_entry_signal_requires_selected_strike_quote(self) -> None:
        prior = os.environ.get("STRATEGY_REDIS_PUBLISH_ENABLED")
        os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = "0"
        try:
            engine = DeterministicRuleEngine()
            vote = StrategyVote(
                strategy_name="TEST",
                snapshot_id="snap-missing-quote",
                timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
                trade_date="2026-02-28",
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=0.90,
                reason="missing strike quote",
                proposed_strike=50000,
                proposed_entry_premium=None,
            )
            snap = _snapshot(
                snapshot_id="snap-missing-quote",
                ts="2026-02-28T10:00:00+00:00",
                ce_price=200.0,
                pe_price=180.0,
                atm_strike=50100,
                strikes=[{"strike": 50100.0, "ce_ltp": 200.0, "pe_ltp": 180.0}],
            )
            regime_signal = RegimeSignal(regime=Regime.TRENDING, confidence=0.90, reason="test", evidence={})

            signal = engine._build_entry_signal(vote, snap, engine._risk.context, [vote], regime_signal)

            self.assertIsNone(signal)
        finally:
            if prior is None:
                os.environ.pop("STRATEGY_REDIS_PUBLISH_ENABLED", None)
            else:
                os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = prior

    def test_position_uses_selected_entry_strategy_name(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc)
        signal = TradeSignal(
            signal_id="sig-owner",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            entry_strategy_name="OI_BUILDUP",
            reason="[TRENDING] OI_BUILDUP: test",
            votes=[
                StrategyVote(
                    strategy_name="ORB",
                    snapshot_id="snap-open",
                    timestamp=ts,
                    trade_date="2026-02-28",
                    signal_type=SignalType.ENTRY,
                    direction=Direction.CE,
                    confidence=0.70,
                    reason="other vote",
                )
            ],
        )

        position = tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        self.assertEqual(position.entry_strategy, "OI_BUILDUP")

    def test_position_tracks_entry_regime_name(self) -> None:
        tracker = PositionTracker()
        tracker.on_session_start(date(2026, 2, 28))
        ts = datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc)
        signal = TradeSignal(
            signal_id="sig-regime",
            timestamp=ts,
            snapshot_id="snap-open",
            signal_type=SignalType.ENTRY,
            direction="CE",
            strike=50000,
            entry_premium=100.0,
            entry_strategy_name="OI_BUILDUP",
            entry_regime_name="PRE_EXPIRY",
            reason="[PRE_EXPIRY] OI_BUILDUP: test",
        )

        position = tracker.open_position(signal, _snapshot(snapshot_id="snap-open", ts=ts.isoformat(), ce_price=100.0))

        self.assertEqual(position.entry_regime, "PRE_EXPIRY")

    def test_default_router_excludes_expiry_max_pain_entries(self) -> None:
        router = StrategyRouter()

        expiry_entries = router.summary()["EXPIRY"]
        sideways_entries = router.summary()["SIDEWAYS"]
        exit_universal = router.summary()["EXIT_UNIVERSAL"]

        self.assertNotIn("EXPIRY_MAX_PAIN", expiry_entries)
        self.assertNotIn("EXPIRY_MAX_PAIN", sideways_entries)
        self.assertNotIn("EXPIRY_MAX_PAIN", exit_universal)

    def test_entry_policy_uses_next_ranked_vote_when_top_candidate_is_blocked(self) -> None:
        prior = os.environ.get("STRATEGY_REDIS_PUBLISH_ENABLED")
        os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = "0"
        try:
            engine = DeterministicRuleEngine(entry_policy=self._FakeEntryPolicy(blocked={"ORB"}))
            snap = _snapshot(snapshot_id="snap-1", ts="2026-02-28T10:00:00+00:00", ce_price=100.0)
            regime_signal = RegimeSignal(regime=Regime.TRENDING, confidence=0.90, reason="test", evidence={})
            orb_vote = StrategyVote(
                strategy_name="ORB",
                snapshot_id="snap-1",
                timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
                trade_date="2026-02-28",
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=0.82,
                reason="orb",
                proposed_strike=50000,
                proposed_entry_premium=100.0,
            )
            oi_vote = StrategyVote(
                strategy_name="OI_BUILDUP",
                snapshot_id="snap-1",
                timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
                trade_date="2026-02-28",
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=0.78,
                reason="oi",
                proposed_strike=50000,
                proposed_entry_premium=100.0,
            )

            signal = engine._process_entry_votes([orb_vote, oi_vote], snap, engine._risk.context, regime_signal)

            self.assertIsNotNone(signal)
            self.assertEqual(signal.entry_strategy_name, "OI_BUILDUP")
            self.assertFalse(bool(orb_vote.raw_signals.get("_policy_allowed")))
            self.assertTrue(bool(oi_vote.raw_signals.get("_policy_allowed")))
        finally:
            if prior is None:
                os.environ.pop("STRATEGY_REDIS_PUBLISH_ENABLED", None)
            else:
                os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = prior

    def test_direction_conflict_still_blocks_when_policy_cannot_resolve_conflict(self) -> None:
        prior = os.environ.get("STRATEGY_REDIS_PUBLISH_ENABLED")
        os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = "0"
        try:
            engine = DeterministicRuleEngine(entry_policy=self._FakeEntryPolicy())
            snap = _snapshot(snapshot_id="snap-conflict-block", ts="2026-02-28T10:00:00+00:00", ce_price=100.0, pe_price=100.0)
            regime_signal = RegimeSignal(regime=Regime.TRENDING, confidence=0.90, reason="test", evidence={})
            ce_vote = StrategyVote(
                strategy_name="ORB",
                snapshot_id="snap-conflict-block",
                timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
                trade_date="2026-02-28",
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=0.82,
                reason="ce vote",
                proposed_strike=50000,
                proposed_entry_premium=100.0,
            )
            pe_vote = StrategyVote(
                strategy_name="EMA_CROSSOVER",
                snapshot_id="snap-conflict-block",
                timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
                trade_date="2026-02-28",
                signal_type=SignalType.ENTRY,
                direction=Direction.PE,
                confidence=0.78,
                reason="pe vote",
                proposed_strike=50000,
                proposed_entry_premium=100.0,
            )

            signal = engine._process_entry_votes([ce_vote, pe_vote], snap, engine._risk.context, regime_signal)

            self.assertIsNone(signal)
        finally:
            if prior is None:
                os.environ.pop("STRATEGY_REDIS_PUBLISH_ENABLED", None)
            else:
                os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = prior

    def test_ml_like_policy_resolves_direction_conflict_using_policy_score(self) -> None:
        prior = os.environ.get("STRATEGY_REDIS_PUBLISH_ENABLED")
        os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = "0"
        try:
            engine = DeterministicRuleEngine(entry_policy=self._ConflictResolvingEntryPolicy(ce_score=0.55, pe_score=0.80))
            snap = _snapshot(snapshot_id="snap-conflict-resolve", ts="2026-02-28T10:00:00+00:00", ce_price=100.0, pe_price=100.0)
            regime_signal = RegimeSignal(regime=Regime.TRENDING, confidence=0.90, reason="test", evidence={})
            ce_vote = StrategyVote(
                strategy_name="ORB",
                snapshot_id="snap-conflict-resolve",
                timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
                trade_date="2026-02-28",
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=0.82,
                reason="ce vote",
                proposed_strike=50000,
                proposed_entry_premium=100.0,
            )
            pe_vote = StrategyVote(
                strategy_name="EMA_CROSSOVER",
                snapshot_id="snap-conflict-resolve",
                timestamp=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
                trade_date="2026-02-28",
                signal_type=SignalType.ENTRY,
                direction=Direction.PE,
                confidence=0.78,
                reason="pe vote",
                proposed_strike=50000,
                proposed_entry_premium=100.0,
            )

            signal = engine._process_entry_votes([ce_vote, pe_vote], snap, engine._risk.context, regime_signal)

            self.assertIsNotNone(signal)
            self.assertEqual(signal.direction, "PE")
            self.assertEqual(signal.entry_strategy_name, "EMA_CROSSOVER")
            self.assertTrue(bool(ce_vote.raw_signals.get("_policy_allowed")))
            self.assertTrue(bool(pe_vote.raw_signals.get("_policy_allowed")))
        finally:
            if prior is None:
                os.environ.pop("STRATEGY_REDIS_PUBLISH_ENABLED", None)
            else:
                os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = prior

    def test_set_run_context_resets_policy_to_default_when_override_absent(self) -> None:
        engine = DeterministicRuleEngine(policy_config=PolicyConfig(iv_pct_hard_max=70.0))

        engine.set_run_context("run-1", {"policy_config": {"iv_pct_hard_max": 60.0}})
        self.assertAlmostEqual(engine._entry_policy.config.iv_pct_hard_max, 60.0, places=6)

        engine.set_run_context("run-2", {})
        self.assertAlmostEqual(engine._entry_policy.config.iv_pct_hard_max, 70.0, places=6)

    def test_owner_exit_vote_is_preferred_over_non_owner(self) -> None:
        engine = DeterministicRuleEngine(router=StrategyRouter())
        position = PositionContext(
            position_id="p1",
            direction="CE",
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc),
            entry_snapshot_id="snap-open",
            lots=1,
            entry_strategy="OI_BUILDUP",
            entry_regime="TRENDING",
        )
        owner_vote = StrategyVote(
            strategy_name="OI_BUILDUP",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.65,
            reason="owner exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )
        non_owner_vote = StrategyVote(
            strategy_name="EMA_CROSSOVER",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.90,
            reason="non-owner exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )

        selected = engine._select_exit_vote([owner_vote, non_owner_vote], position)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.strategy_name, "OI_BUILDUP")

    def test_pre_expiry_oi_allows_orb_helper_exit(self) -> None:
        engine = DeterministicRuleEngine(router=StrategyRouter())
        position = PositionContext(
            position_id="p2",
            direction="CE",
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc),
            entry_snapshot_id="snap-open",
            lots=1,
            entry_strategy="OI_BUILDUP",
            entry_regime="PRE_EXPIRY",
        )
        orb_vote = StrategyVote(
            strategy_name="ORB",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.65,
            reason="orb helper exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )
        ema_vote = StrategyVote(
            strategy_name="EMA_CROSSOVER",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.70,
            reason="ema exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )

        selected = engine._select_exit_vote([orb_vote, ema_vote], position)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.strategy_name, "ORB")

    def test_strong_non_owner_exit_requires_high_confidence(self) -> None:
        engine = DeterministicRuleEngine(router=StrategyRouter())
        position = PositionContext(
            position_id="p3",
            direction="CE",
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc),
            entry_snapshot_id="snap-open",
            lots=1,
            entry_strategy="OI_BUILDUP",
            entry_regime="TRENDING",
        )
        weak_non_owner = StrategyVote(
            strategy_name="EMA_CROSSOVER",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.79,
            reason="weak exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )
        strong_non_owner = StrategyVote(
            strategy_name="EMA_CROSSOVER",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.81,
            reason="strong exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )

        weak_selected = engine._select_exit_vote([weak_non_owner], position)
        strong_selected = engine._select_exit_vote([strong_non_owner], position)

        self.assertIsNone(weak_selected)
        self.assertIsNotNone(strong_selected)
        self.assertEqual(strong_selected.strategy_name, "EMA_CROSSOVER")

    def test_regime_shift_exit_deferred_when_trailing_is_active(self) -> None:
        engine = DeterministicRuleEngine(router=StrategyRouter())
        position = PositionContext(
            position_id="p4",
            direction="CE",
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc),
            entry_snapshot_id="snap-open",
            lots=1,
            entry_strategy="EMA_CROSSOVER",
            entry_regime="TRENDING",
            trailing_enabled=True,
            trailing_active=True,
        )
        vote = StrategyVote(
            strategy_name="EMA_CROSSOVER",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.90,
            reason="regime exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )

        called = {"force_exit": 0}

        def _force_exit(*args, **kwargs):
            called["force_exit"] += 1
            return None

        engine._tracker.force_exit = _force_exit  # type: ignore[method-assign]

        signal = engine._process_exit_votes([vote], _snapshot(snapshot_id="snap-1", ts="2026-02-28T09:31:00+00:00", ce_price=99.0), position)

        self.assertIsNone(signal)
        self.assertEqual(called["force_exit"], 0)

    def test_regime_shift_exit_not_deferred_when_orb_priority_disabled(self) -> None:
        engine = DeterministicRuleEngine(router=StrategyRouter())
        position = PositionContext(
            position_id="p5",
            direction="CE",
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc),
            entry_snapshot_id="snap-open",
            lots=1,
            entry_strategy="ORB",
            entry_regime="PRE_EXPIRY",
            orb_trail_active=True,
            orb_trail_priority_over_regime=False,
        )
        vote = StrategyVote(
            strategy_name="ORB",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.90,
            reason="regime exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )

        self.assertFalse(engine._should_defer_regime_shift_exit(position, vote))

    def test_regime_shift_requires_confirmation_bars(self) -> None:
        engine = DeterministicRuleEngine(router=StrategyRouter())
        engine.set_run_context("run-confirm", {"risk_config": {"regime_shift_confirm_bars": 2}})
        position = PositionContext(
            position_id="p6",
            direction="CE",
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc),
            entry_snapshot_id="snap-open",
            lots=1,
            entry_strategy="EMA_CROSSOVER",
            entry_regime="TRENDING",
            pnl_pct=-0.02,
        )
        vote = StrategyVote(
            strategy_name="EMA_CROSSOVER",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.90,
            reason="regime exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )

        calls = {"n": 0}

        def _force_exit(*args, **kwargs):
            calls["n"] += 1
            return TradeSignal(
                signal_id="sig-exit",
                timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
                snapshot_id="snap-1",
                signal_type=SignalType.EXIT,
                direction="EXIT",
                exit_reason=ExitReason.REGIME_SHIFT,
            )

        engine._tracker.force_exit = _force_exit  # type: ignore[method-assign]
        engine._handle_position_closed = lambda *args, **kwargs: None  # type: ignore[method-assign]

        s1 = engine._process_exit_votes([vote], _snapshot(snapshot_id="snap-1", ts="2026-02-28T09:31:00+00:00", ce_price=99.0), position)
        s2 = engine._process_exit_votes([vote], _snapshot(snapshot_id="snap-2", ts="2026-02-28T09:32:00+00:00", ce_price=98.0), position)

        self.assertIsNone(s1)
        self.assertIsNotNone(s2)
        self.assertEqual(calls["n"], 1)

    def test_regime_shift_profit_hold_suppresses_exit(self) -> None:
        engine = DeterministicRuleEngine(router=StrategyRouter())
        engine.set_run_context("run-profit-hold", {"risk_config": {"regime_shift_min_profit_hold_pct": 0.10}})
        position = PositionContext(
            position_id="p7",
            direction="CE",
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2026, 2, 28, 9, 30, tzinfo=timezone.utc),
            entry_snapshot_id="snap-open",
            lots=1,
            entry_strategy="EMA_CROSSOVER",
            entry_regime="TRENDING",
            pnl_pct=0.12,
        )
        vote = StrategyVote(
            strategy_name="EMA_CROSSOVER",
            snapshot_id="snap-1",
            timestamp=datetime(2026, 2, 28, 9, 31, tzinfo=timezone.utc),
            trade_date="2026-02-28",
            signal_type=SignalType.EXIT,
            direction=Direction.EXIT,
            confidence=0.90,
            reason="regime exit",
            exit_reason=ExitReason.REGIME_SHIFT,
        )

        called = {"force_exit": 0}

        def _force_exit(*args, **kwargs):
            called["force_exit"] += 1
            return None

        engine._tracker.force_exit = _force_exit  # type: ignore[method-assign]

        signal = engine._process_exit_votes([vote], _snapshot(snapshot_id="snap-1", ts="2026-02-28T09:31:00+00:00", ce_price=112.0), position)

        self.assertIsNone(signal)
        self.assertEqual(called["force_exit"], 0)

    def test_ml_entry_policy_prefers_strategy_specific_threshold_over_default(self) -> None:
        policy = MLEntryPolicy.__new__(MLEntryPolicy)
        policy._default_threshold = 0.60
        policy._strategy_threshold_overrides = {"OI_BUILDUP": 0.50}
        policy._strategy_regime_threshold_overrides = {("TRENDING", "EMA_CROSSOVER"): 0.80}

        self.assertAlmostEqual(
            policy._resolve_threshold(
                strategy_name="OI_BUILDUP",
                regime_name="TRENDING",
                segment={"threshold": 0.65},
            ),
            0.50,
            places=6,
        )
        self.assertAlmostEqual(
            policy._resolve_threshold(
                strategy_name="EMA_CROSSOVER",
                regime_name="TRENDING",
                segment={"threshold": 0.65},
            ),
            0.80,
            places=6,
        )
        self.assertAlmostEqual(
            policy._resolve_threshold(
                strategy_name="ORB",
                regime_name="PRE_EXPIRY",
                segment={"threshold": 0.65},
            ),
            0.60,
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
