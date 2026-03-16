import os
import unittest
from datetime import datetime

from contracts_app import IST_ZONE
from strategy_app.contracts import Direction, SignalType, StrategyVote
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.engines.snapshot_accessor import SnapshotAccessor


def _snapshot_with_strikes(strikes: list[dict], *, atm_strike: int = 50000) -> SnapshotAccessor:
    ts = datetime(2026, 3, 6, 10, 0, tzinfo=IST_ZONE).isoformat()
    return SnapshotAccessor(
        {
            "snapshot_id": "snap-strike-policy",
            "session_context": {
                "snapshot_id": "snap-strike-policy",
                "timestamp": ts,
                "date": ts[:10],
                "session_phase": "ACTIVE",
                "days_to_expiry": 1,
            },
            "chain_aggregates": {"atm_strike": atm_strike, "strike_count": len(strikes)},
            "atm_options": {"atm_ce_close": 100.0, "atm_pe_close": 100.0},
            "strikes": strikes,
        }
    )


def _entry_vote(direction: Direction) -> StrategyVote:
    ts = datetime(2026, 3, 6, 10, 0, tzinfo=IST_ZONE)
    return StrategyVote(
        strategy_name="EMA_CROSSOVER",
        snapshot_id="snap-strike-policy",
        timestamp=ts,
        trade_date="2026-03-06",
        signal_type=SignalType.ENTRY,
        direction=direction,
        confidence=0.80,
        reason="test",
        proposed_strike=50000,
        proposed_entry_premium=100.0,
    )


class StrikeSelectionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = dict(os.environ)
        os.environ["STRATEGY_STRIKE_SELECTION_POLICY"] = "oi_volume_ranked"
        os.environ["STRATEGY_STRIKE_MAX_OTM_STEPS"] = "2"
        os.environ["STRATEGY_STRIKE_MIN_OI"] = "10000"
        os.environ["STRATEGY_STRIKE_MIN_VOLUME"] = "10000"
        os.environ["STRATEGY_STRIKE_LIQUIDITY_WEIGHT"] = "1.0"
        os.environ["STRATEGY_STRIKE_AFFORDABILITY_WEIGHT"] = "0.25"
        os.environ["STRATEGY_STRIKE_DISTANCE_PENALTY"] = "0.05"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_selects_otm_when_liquidity_is_stronger(self) -> None:
        engine = DeterministicRuleEngine(min_confidence=0.65)
        snap = _snapshot_with_strikes(
            [
                {"strike": 50000.0, "ce_ltp": 100.0, "ce_oi": 15000.0, "ce_volume": 15000.0},
                {"strike": 50100.0, "ce_ltp": 78.0, "ce_oi": 80000.0, "ce_volume": 90000.0},
                {"strike": 50200.0, "ce_ltp": 62.0, "ce_oi": 9000.0, "ce_volume": 9000.0},
            ],
            atm_strike=50000,
        )
        vote = _entry_vote(Direction.CE)

        engine._apply_strike_selection(vote, snap)

        self.assertEqual(vote.proposed_strike, 50100)
        self.assertAlmostEqual(float(vote.proposed_entry_premium or 0.0), 78.0, places=6)
        self.assertEqual(vote.raw_signals.get("_strike_policy"), "oi_volume_ranked")

    def test_falls_back_to_atm_when_otm_fails_liquidity_floor(self) -> None:
        engine = DeterministicRuleEngine(min_confidence=0.65)
        snap = _snapshot_with_strikes(
            [
                {"strike": 50000.0, "pe_ltp": 102.0, "pe_oi": 20000.0, "pe_volume": 18000.0},
                {"strike": 49900.0, "pe_ltp": 80.0, "pe_oi": 2000.0, "pe_volume": 1500.0},
                {"strike": 49800.0, "pe_ltp": 66.0, "pe_oi": 1000.0, "pe_volume": 1200.0},
            ],
            atm_strike=50000,
        )
        vote = _entry_vote(Direction.PE)

        engine._apply_strike_selection(vote, snap)

        self.assertEqual(vote.proposed_strike, 50000)
        self.assertAlmostEqual(float(vote.proposed_entry_premium or 0.0), 102.0, places=6)


if __name__ == "__main__":
    unittest.main()
