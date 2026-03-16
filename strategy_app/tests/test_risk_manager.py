import unittest
from datetime import date
from unittest import mock

from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.risk.manager import RiskManager


def _snap(*, ts: str, vix_intraday_chg: float | None = None, vix_spike_flag: bool = False) -> SnapshotAccessor:
    return SnapshotAccessor(
        {
            "session_context": {
                "snapshot_id": f"snap-{ts}",
                "timestamp": ts,
                "date": ts[:10],
                "session_phase": "ACTIVE",
            },
            "vix_context": {
                "vix_intraday_chg": vix_intraday_chg,
                "vix_spike_flag": vix_spike_flag,
            },
        }
    )


class RiskManagerTests(unittest.TestCase):
    def test_budget_per_trade_sizing_uses_notional_budget(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "RISK_LOT_SIZING_MODE": "budget_per_trade",
                "RISK_NOTIONAL_PER_TRADE": "100000",
                "RISK_LOT_BUDGET_USES_LOT_SIZE": "1",
                "RISK_MAX_LOTS_PER_TRADE": "999",
            },
            clear=False,
        ):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            # 100000 / (200 * 15) = 33 lots
            lots = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.40, confidence=0.25)
            self.assertEqual(lots, 33)

    def test_aggressive_safe_profile_applies_defaults(self) -> None:
        with mock.patch.dict("os.environ", {"RISK_PROFILE": "aggressive_safe_v1"}, clear=False):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            self.assertEqual(mgr.context.max_consecutive_losses, 3)
            self.assertAlmostEqual(float(mgr.context.max_daily_loss_pct), 0.02, places=8)
            self.assertEqual(mgr.context.max_lots_per_trade, 20)
            # 50000 / (250 * 15) -> 13 lots
            lots = mgr.compute_lots(entry_premium=250.0, stop_loss_pct=0.40, confidence=0.25)
            self.assertEqual(lots, 13)

    def test_profile_can_be_overridden_by_env(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "RISK_PROFILE": "aggressive_safe_v1",
                "RISK_NOTIONAL_PER_TRADE": "70000",
                "RISK_MAX_LOTS_PER_TRADE": "10",
            },
            clear=False,
        ):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            # 70000 / (200 * 15) -> 23, then capped to 10
            lots = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.40, confidence=1.0)
            self.assertEqual(lots, 10)

    def test_vix_resume_requires_30min_continuous_below_threshold(self) -> None:
        mgr = RiskManager()
        mgr.on_session_start(date(2026, 3, 5))

        mgr.update(_snap(ts="2026-03-05T10:00:00+05:30", vix_intraday_chg=16.0), None)
        self.assertTrue(mgr.context.vix_spike_halt)

        mgr.update(_snap(ts="2026-03-05T10:05:00+05:30", vix_intraday_chg=7.0), None)
        self.assertTrue(mgr.context.vix_spike_halt)
        self.assertIsNotNone(mgr.context.vix_below_resume_since)

        mgr.update(_snap(ts="2026-03-05T10:34:00+05:30", vix_intraday_chg=7.0), None)
        self.assertTrue(mgr.context.vix_spike_halt)

        mgr.update(_snap(ts="2026-03-05T10:35:00+05:30", vix_intraday_chg=7.0), None)
        self.assertFalse(mgr.context.vix_spike_halt)
        self.assertTrue(mgr.post_halt_resume_boost_available)
        self.assertIsNotNone(mgr.context.vix_last_resume_at)

    def test_vix_resume_cooldown_resets_on_rebreach(self) -> None:
        mgr = RiskManager()
        mgr.on_session_start(date(2026, 3, 5))

        mgr.update(_snap(ts="2026-03-05T10:00:00+05:30", vix_intraday_chg=16.0), None)
        mgr.update(_snap(ts="2026-03-05T10:05:00+05:30", vix_intraday_chg=7.0), None)
        mgr.update(_snap(ts="2026-03-05T10:20:00+05:30", vix_intraday_chg=7.0), None)
        self.assertTrue(mgr.context.vix_spike_halt)
        self.assertIsNotNone(mgr.context.vix_below_resume_since)

        mgr.update(_snap(ts="2026-03-05T10:21:00+05:30", vix_intraday_chg=9.0), None)
        self.assertTrue(mgr.context.vix_spike_halt)
        self.assertIsNone(mgr.context.vix_below_resume_since)

        mgr.update(_snap(ts="2026-03-05T10:45:00+05:30", vix_intraday_chg=7.0), None)
        mgr.update(_snap(ts="2026-03-05T11:14:00+05:30", vix_intraday_chg=7.0), None)
        self.assertTrue(mgr.context.vix_spike_halt)

        mgr.update(_snap(ts="2026-03-05T11:15:00+05:30", vix_intraday_chg=7.0), None)
        self.assertFalse(mgr.context.vix_spike_halt)

    def test_post_halt_resume_boost_consumed_once(self) -> None:
        mgr = RiskManager()
        mgr.on_session_start(date(2026, 3, 5))
        mgr.update(_snap(ts="2026-03-05T10:00:00+05:30", vix_intraday_chg=16.0), None)
        mgr.update(_snap(ts="2026-03-05T10:05:00+05:30", vix_intraday_chg=7.0), None)
        mgr.update(_snap(ts="2026-03-05T10:35:00+05:30", vix_intraday_chg=7.0), None)

        self.assertTrue(mgr.post_halt_resume_boost_available)
        self.assertTrue(mgr.consume_post_halt_resume_boost())
        self.assertFalse(mgr.post_halt_resume_boost_available)
        self.assertFalse(mgr.consume_post_halt_resume_boost())


if __name__ == "__main__":
    unittest.main()
