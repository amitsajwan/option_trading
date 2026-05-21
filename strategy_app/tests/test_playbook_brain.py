from __future__ import annotations

import unittest
from datetime import datetime

from strategy_app.contracts import PositionContext
from strategy_app.engines.playbook_brain import (
    PLAYBOOK_EXIT_KEY,
    evaluate_playbook_exit,
    playbook_exit_metrics,
)
from strategy_app.engines.r1s_rule_runtime import load_rule, row_passes_entry
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.strategies.rule_top3_short_ce import PlaybookV1ShortCeStrategy


def _quality_payload(*, minute: int, ret_5m: float = -0.001, vwap: float = -0.0008) -> dict:
    return {
        "snapshot_id": f"snap-{minute}",
        "session_context": {
            "snapshot_id": f"snap-{minute}",
            "timestamp": "2024-05-15T10:30:00+05:30",
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
        "ctx_is_high_vix_day": 0.0,
        "chain_aggregates": {"atm_strike": 50000},
        "atm_options": {"atm_ce_close": 120.0},
    }


class PlaybookBrainTests(unittest.TestCase):
    def test_high_vix_disqualifies_quality_entry(self) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        rule = load_rule(
            str(repo / "ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_quality_thesis.json")
        )
        snap = SnapshotAccessor(_quality_payload(minute=30))
        self.assertTrue(row_passes_entry(snap, rule))
        high_vix = _quality_payload(minute=30)
        high_vix["ctx_is_high_vix_day"] = 1.0
        self.assertFalse(row_passes_entry(SnapshotAccessor(high_vix), rule))

    def test_thesis_exit_on_vwap_reclaim(self) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        rule = load_rule(
            str(repo / "ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_quality_thesis.json")
        )
        position = PositionContext(
            position_id="p1",
            direction="CE",
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2024, 5, 15, 10, 0),
            entry_snapshot_id="s0",
            lots=1,
            position_side="SHORT",
            pnl_pct=0.05,
            mfe_pct=0.10,
            playbook_exit_policy=playbook_exit_metrics(rule),
        )
        snap = SnapshotAccessor(
            {
                "snapshot_id": "s1",
                "session_context": {
                    "timestamp": "2024-05-15T11:00:00+05:30",
                    "date": "2024-05-15",
                    "minutes_since_open": 60,
                },
                "futures_derived": {"price_vs_vwap": 0.001},
            }
        )
        hit = evaluate_playbook_exit(position, snap)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit[1], "signal:vwap_distance")

    def test_playbook_strategy_emits_brain_flag(self) -> None:
        from datetime import date

        strategy = PlaybookV1ShortCeStrategy()
        strategy.on_session_start(date(2024, 5, 15))
        vote = strategy.evaluate(_quality_payload(minute=30), None, None)
        self.assertIsNotNone(vote)
        assert vote is not None
        self.assertTrue(vote.raw_signals.get("_playbook_brain"))
        self.assertIn(PLAYBOOK_EXIT_KEY, vote.raw_signals)


if __name__ == "__main__":
    unittest.main()
