import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from strategy_app.contracts import Direction, ExitReason, SignalType, StrategyVote, TradeSignal
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.logging.signal_logger import SignalLogger
from strategy_app.position.tracker import PositionTracker


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, topic: str, event: dict) -> None:
        self.events.append((topic, event))


class SignalLoggerParitySnapshotTests(unittest.TestCase):
    def _fixture(self, name: str) -> dict:
        path = Path(__file__).resolve().parent / "fixtures" / name
        return json.loads(path.read_text(encoding="utf-8"))

    def test_vote_signal_position_rows_match_frozen_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            logger = SignalLogger(run_dir)
            logger._publisher = _RecordingPublisher()  # type: ignore[attr-defined]
            logger.set_run_context(
                "run-parity",
                {
                    "engine_mode": "deterministic",
                    "strategy_family_version": "DET_V1",
                    "strategy_profile_id": "det_core_v2",
                },
            )
            vote = StrategyVote(
                strategy_name="EMA_CROSSOVER",
                snapshot_id="snap-100",
                timestamp=datetime(2026, 3, 7, 9, 30, tzinfo=timezone.utc),
                trade_date="2026-03-07",
                signal_type=SignalType.ENTRY,
                direction=Direction.CE,
                confidence=0.72,
                reason="allowed score=0.71",
                raw_signals={
                    "_policy_allowed": True,
                    "_policy_score": 0.71,
                    "_policy_reason": "allowed score=0.71",
                    "_policy_checks": {},
                },
            )
            hold_signal = TradeSignal(
                signal_id="sig-hold-1",
                timestamp=datetime(2026, 3, 7, 9, 31, tzinfo=timezone.utc),
                snapshot_id="snap-101",
                signal_type=SignalType.HOLD,
                source="ML_PURE",
                reason="ml_pure_hold:feature_stale",
                confidence=0.62,
            )
            entry_signal = TradeSignal(
                signal_id="sig-entry-1",
                timestamp=datetime(2026, 3, 7, 9, 32, tzinfo=timezone.utc),
                snapshot_id="snap-102",
                signal_type=SignalType.ENTRY,
                direction="CE",
                strike=60200,
                entry_premium=120.0,
                max_hold_bars=15,
                stop_loss_pct=0.05,
                target_pct=0.2,
                source="RULE",
                confidence=0.74,
                reason="entry",
            )
            tracker = PositionTracker()
            position = tracker.open_position(
                entry_signal,
                SnapshotAccessor(
                    {
                        "session_context": {
                            "snapshot_id": "snap-102",
                            "timestamp": "2026-03-07T09:32:00+00:00",
                            "date": "2026-03-07",
                            "session_phase": "ACTIVE",
                        }
                    }
                ),
            )
            position.position_id = "pos-1"
            position.current_premium = 114.0
            position.pnl_pct = -0.05
            position.mfe_pct = 0.01
            position.mae_pct = -0.05
            position.bars_held = 1
            position.high_water_premium = 121.0

            logger.log_vote(vote)
            logger.log_signal(hold_signal, acted_on=False)
            logger.log_position_open(entry_signal, position)
            logger.log_position_manage(
                position=position,
                timestamp=datetime(2026, 3, 7, 9, 33, tzinfo=timezone.utc),
                snapshot_id="snap-103",
            )
            position.bars_held = 2
            logger.log_position_close(
                exit_signal=TradeSignal(
                    signal_id="sig-exit-1",
                    timestamp=datetime(2026, 3, 7, 9, 34, tzinfo=timezone.utc),
                    snapshot_id="snap-104",
                    signal_type=SignalType.EXIT,
                    direction="CE",
                    strike=60200,
                    position_id="pos-1",
                    source="RULE",
                    reason="exit",
                    exit_reason=ExitReason.TIME_STOP,
                ),
                position=position,
            )

            vote_row = json.loads((run_dir / "votes.jsonl").read_text(encoding="utf-8").splitlines()[0])
            signal_row = json.loads((run_dir / "signals.jsonl").read_text(encoding="utf-8").splitlines()[0])
            position_rows = [
                json.loads(line)
                for line in (run_dir / "positions.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(vote_row, self._fixture("signal_logger_vote_snapshot.json"))
            self.assertEqual(signal_row, self._fixture("signal_logger_signal_snapshot.json"))
            self.assertEqual(position_rows[0], self._fixture("signal_logger_position_snapshot.json"))
            self.assertEqual(position_rows[1], self._fixture("signal_logger_position_manage_snapshot.json"))
            self.assertEqual(position_rows[2], self._fixture("signal_logger_position_close_snapshot.json"))


if __name__ == "__main__":
    unittest.main()
