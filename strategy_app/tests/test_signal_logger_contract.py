import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from contracts_app import strategy_decision_trace_topic, strategy_position_topic
from strategy_app.contracts import Direction, ExitReason, PositionContext, SignalType, StrategyVote, TradeSignal
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.logging.signal_logger import SignalLogger
from strategy_app.position.tracker import PositionTracker


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, topic: str, event: dict) -> None:
        self.events.append((topic, event))


class SignalLoggerContractTests(unittest.TestCase):
    def test_logs_engine_aware_vote_and_signal_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            logger = SignalLogger(root)
            logger.set_run_context(
                "run-1",
                {
                    "engine_mode": "ml_pure",
                    "strategy_family_version": "ML_PURE_STAGED_V1",
                    "strategy_profile_id": "ml_pure_staged_v1",
                },
            )
            vote = StrategyVote(
                strategy_name="ML_PURE_STAGED",
                snapshot_id="snap-1",
                timestamp=datetime(2026, 3, 7, 9, 30, tzinfo=timezone.utc),
                trade_date="2026-03-07",
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=0.81,
                reason="ml_pure hold reason=low_edge_conflict",
                decision_reason_code="low_edge_conflict",
                decision_metrics={"ce_prob": 0.62, "pe_prob": 0.60, "edge": 0.02},
            )
            signal = TradeSignal(
                signal_id="sig-1",
                timestamp=datetime(2026, 3, 7, 9, 30, tzinfo=timezone.utc),
                snapshot_id="snap-1",
                signal_type=SignalType.HOLD,
                source="ML_PURE",
                reason="ml_pure_hold:feature_stale",
                decision_reason_code="feature_stale",
                decision_metrics={"confidence": 0.62},
            )

            logger.log_vote(vote)
            logger.log_signal(signal, acted_on=False)

            vote_row = json.loads((root / "votes.jsonl").read_text(encoding="utf-8").strip().splitlines()[0])
            signal_row = json.loads((root / "signals.jsonl").read_text(encoding="utf-8").strip().splitlines()[0])

            self.assertEqual(vote_row["engine_mode"], "ml_pure")
            self.assertEqual(vote_row["strategy_family_version"], "ML_PURE_STAGED_V1")
            self.assertEqual(vote_row["strategy_profile_id"], "ml_pure_staged_v1")
            self.assertEqual(vote_row["decision_reason_code"], "low_edge_conflict")
            self.assertIsInstance(vote_row["decision_metrics"], dict)

            self.assertEqual(signal_row["engine_mode"], "ml_pure")
            self.assertEqual(signal_row["decision_mode"], "ml_staged")
            self.assertEqual(signal_row["decision_reason_code"], "feature_stale")
            self.assertEqual(signal_row["strategy_profile_id"], "ml_pure_staged_v1")
            self.assertEqual(signal_row["acted_on"], False)

    def test_position_lifecycle_preserves_entry_signal_id_and_resolved_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            logger = SignalLogger(root)
            publisher = _RecordingPublisher()
            logger._publisher = publisher  # type: ignore[attr-defined]
            logger.set_run_context(
                "run-2",
                {
                    "engine_mode": "ml_pure",
                    "strategy_family_version": "ML_PURE_STAGED_V1",
                    "strategy_profile_id": "ml_pure_staged_v1",
                },
            )
            entry_signal = TradeSignal(
                signal_id="sig-entry-2",
                timestamp=datetime(2026, 3, 7, 9, 30, tzinfo=timezone.utc),
                snapshot_id="snap-entry-2",
                signal_type=SignalType.ENTRY,
                direction="CE",
                strike=60200,
                entry_premium=120.0,
                max_hold_bars=15,
                source="ML_PURE",
                confidence=0.74,
                reason="entry",
                decision_metrics={"ce_prob": 0.62, "pe_prob": 0.60},
            )
            tracker = PositionTracker()
            position = tracker.open_position(
                entry_signal,
                SnapshotAccessor(
                    {
                        "session_context": {
                            "snapshot_id": "snap-entry-2",
                            "timestamp": "2026-03-07T09:30:00+00:00",
                            "date": "2026-03-07",
                            "session_phase": "ACTIVE",
                        }
                    }
                ),
            )
            position.position_id = "pos-2"
            original_metrics = dict(position.decision_metrics)
            original_engine_mode = position.engine_mode
            original_decision_mode = position.decision_mode
            original_reason_code = position.decision_reason_code
            original_profile_id = position.strategy_profile_id

            logger.log_position_open(entry_signal, position)
            position.current_premium = 118.0
            position.pnl_pct = -0.0166667
            position.mfe_pct = 0.02
            position.mae_pct = -0.03
            position.bars_held = 1
            position.high_water_premium = 122.0
            logger.log_position_manage(
                position=position,
                timestamp=datetime(2026, 3, 7, 9, 31, tzinfo=timezone.utc),
                snapshot_id="snap-manage-2",
            )
            exit_signal = TradeSignal(
                signal_id="sig-exit-2",
                timestamp=datetime(2026, 3, 7, 9, 32, tzinfo=timezone.utc),
                snapshot_id="snap-exit-2",
                signal_type=SignalType.EXIT,
                direction="CE",
                strike=60200,
                position_id="pos-2",
                source="ML_PURE",
                reason="exit",
                exit_reason=ExitReason.TIME_STOP,
            )
            logger.log_position_close(
                exit_signal=exit_signal,
                position=position,
                entry_premium=120.0,
                exit_premium=118.0,
                pnl_pct=-0.0166667,
                mfe_pct=0.02,
                mae_pct=-0.03,
                bars_held=2,
                stop_loss_pct=0.05,
                stop_price=114.0,
                high_water_premium=122.0,
                target_pct=0.20,
                trailing_enabled=False,
                trailing_activation_pct=0.10,
                trailing_offset_pct=0.05,
                trailing_lock_breakeven=True,
                trailing_active=False,
                orb_trail_activation_mfe=0.15,
                orb_trail_offset_pct=0.08,
                orb_trail_min_lock_pct=0.05,
                orb_trail_priority_over_regime=True,
                orb_trail_regime_filter=None,
                orb_trail_active=False,
                orb_trail_stop_price=None,
                oi_trail_activation_mfe=0.15,
                oi_trail_offset_pct=0.08,
                oi_trail_min_lock_pct=0.05,
                oi_trail_priority_over_regime=True,
                oi_trail_regime_filter=None,
                oi_trail_active=False,
                oi_trail_stop_price=None,
            )

            position_rows = [
                json.loads(line)
                for line in (root / "positions.jsonl").read_text(encoding="utf-8").strip().splitlines()
            ]
            position_events = [event for topic, event in publisher.events if topic == strategy_position_topic()]

            self.assertEqual(position.signal_id, "sig-entry-2")
            self.assertEqual(position.decision_metrics, original_metrics)
            self.assertEqual(position.engine_mode, original_engine_mode)
            self.assertEqual(position.decision_mode, original_decision_mode)
            self.assertEqual(position.decision_reason_code, original_reason_code)
            self.assertEqual(position.strategy_profile_id, original_profile_id)
            self.assertAlmostEqual(float(position.decision_metrics["confidence"]), 0.74, places=6)
            self.assertAlmostEqual(float(position.decision_metrics["edge"]), 0.02, places=6)
            self.assertEqual(position_rows[0]["signal_id"], "sig-entry-2")
            self.assertEqual(position_rows[1]["signal_id"], "sig-entry-2")
            self.assertEqual(position_rows[2]["signal_id"], "sig-entry-2")
            self.assertEqual(position_rows[0]["snapshot_id"], "snap-entry-2")
            self.assertEqual(position_rows[0]["entry_snapshot_id"], "snap-entry-2")
            self.assertEqual(position_rows[1]["snapshot_id"], "snap-manage-2")
            self.assertEqual(position_rows[1]["entry_snapshot_id"], "snap-entry-2")
            self.assertEqual(position_rows[2]["snapshot_id"], "snap-exit-2")
            self.assertEqual(position_rows[2]["entry_snapshot_id"], "snap-entry-2")
            self.assertEqual(position_rows[2]["decision_reason_code"], "time_stop")
            self.assertAlmostEqual(float(position_rows[1]["decision_metrics"]["confidence"]), 0.74, places=6)
            self.assertAlmostEqual(float(position_rows[2]["decision_metrics"]["confidence"]), 0.74, places=6)
            self.assertAlmostEqual(float(position_rows[2]["decision_metrics"]["edge"]), 0.02, places=6)
            self.assertEqual(len(position_events), 3)
            self.assertEqual(position_events[0]["metadata"]["signal_id"], "sig-entry-2")
            self.assertEqual(position_events[0]["metadata"]["snapshot_id"], "snap-entry-2")
            self.assertEqual(position_events[1]["metadata"]["snapshot_id"], "snap-manage-2")
            self.assertEqual(position_events[2]["metadata"]["snapshot_id"], "snap-exit-2")

    def test_logs_decision_trace_to_jsonl_and_pubsub(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            logger = SignalLogger(root)
            publisher = _RecordingPublisher()
            logger._publisher = publisher  # type: ignore[attr-defined]
            logger.set_run_context("trace-run-1", {"engine_mode": "deterministic"})

            trace = {
                "trace_id": "trace-1",
                "snapshot_id": "snap-trace-1",
                "timestamp": datetime(2026, 3, 7, 9, 35, tzinfo=timezone.utc),
                "trade_date_ist": "2026-03-07",
                "run_id": "trace-run-1",
                "engine_mode": "deterministic",
                "decision_mode": "rule_vote",
                "evaluation_type": "entry",
                "final_outcome": "blocked",
                "primary_blocker_gate": "policy_checks",
                "selected_candidate_id": None,
                "position_state": {"has_position": False},
                "risk_state": {"is_halted": False},
                "regime_context": {"regime": "TRENDING"},
                "warmup_context": {"blocked": False},
                "summary_metrics": {"candidate_count": 2},
                "flow_gates": [],
                "candidates": [],
            }

            logger.log_decision_trace(trace)

            row = json.loads((root / "decision_traces.jsonl").read_text(encoding="utf-8").strip().splitlines()[0])
            self.assertEqual(row["trace_id"], "trace-1")
            self.assertEqual(row["engine_mode"], "deterministic")
            self.assertEqual(row["primary_blocker_gate"], "policy_checks")
            trace_events = [event for topic, event in publisher.events if topic == strategy_decision_trace_topic()]
            self.assertEqual(len(trace_events), 1)
            self.assertEqual(trace_events[0]["trace"]["trace_id"], "trace-1")


if __name__ == "__main__":
    unittest.main()
