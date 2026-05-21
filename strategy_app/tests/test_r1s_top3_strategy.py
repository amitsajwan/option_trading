from __future__ import annotations

import unittest
from datetime import date

from strategy_app.contracts import Direction, SignalType
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.strategies.r1s_top3_short_ce import R1sTop3ShortCeStrategy


def _payload(*, minute: int, ret_5m: float = -0.002, vwap: float = -0.001) -> dict:
    return {
        "snapshot_id": f"snap-{minute}",
        "session_context": {
            "snapshot_id": f"snap-{minute}",
            "timestamp": "2024-05-15T10:00:00+05:30",
            "date": "2024-05-15",
            "minutes_since_open": minute,
            "is_expiry_day": False,
        },
        "futures_derived": {
            "fut_return_5m": ret_5m,
            "price_vs_vwap": vwap,
        },
        "ctx_opening_range_ready": 1.0,
        "ctx_opening_range_breakout_down": 1.0,
        "chain_aggregates": {"atm_strike": 50000},
        "atm_options": {"atm_ce_close": 120.0},
    }


class R1sTop3StrategyTests(unittest.TestCase):
    def test_emits_short_ce_entry_when_conditions_met(self) -> None:
        strategy = R1sTop3ShortCeStrategy()
        strategy.on_session_start(date(2024, 5, 15))
        vote = strategy.evaluate(_payload(minute=30), None, None)
        self.assertIsNotNone(vote)
        assert vote is not None
        self.assertEqual(vote.signal_type, SignalType.ENTRY)
        self.assertEqual(vote.direction, Direction.CE)
        self.assertTrue(vote.raw_signals.get("_r1s_short_ce"))

    def test_max_three_entries_per_day(self) -> None:
        strategy = R1sTop3ShortCeStrategy()
        strategy.on_session_start(date(2024, 5, 15))
        votes = []
        for minute in (20, 25, 30, 35, 40):
            vote = strategy.evaluate(_payload(minute=minute, ret_5m=-0.001 * minute), None, None)
            if vote is not None:
                votes.append(vote)
        self.assertLessEqual(len(votes), 3)

    def test_blocks_before_opening_range_window(self) -> None:
        strategy = R1sTop3ShortCeStrategy()
        strategy.on_session_start(date(2024, 5, 15))
        payload = _payload(minute=5)
        payload["session_context"]["minutes_since_open"] = 5
        vote = strategy.evaluate(payload, None, None)
        self.assertIsNone(vote)


if __name__ == "__main__":
    unittest.main()
