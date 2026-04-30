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
            # Budget remains a hard cap. Low confidence is floored to 0.65 of that cap:
            # base_lots = int(100000 / (200 * 15)) = 33 lots, scaled -> int(33 * 0.65) = 21.
            lots = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.40, confidence=0.25)
            self.assertEqual(lots, 21)

    def test_aggressive_safe_profile_applies_defaults(self) -> None:
        with mock.patch.dict("os.environ", {"RISK_PROFILE": "aggressive_safe_v1"}, clear=False):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            self.assertEqual(mgr.context.max_consecutive_losses, 3)
            self.assertAlmostEqual(float(mgr.context.max_daily_loss_pct), 0.02, places=8)
            self.assertEqual(mgr.context.max_lots_per_trade, 20)
            # The profile keeps the configured notional cap and scales down at the floor:
            # int(50000 / (250 * 15)) = 13 lots, scaled -> int(13 * 0.65) = 8.
            lots = mgr.compute_lots(entry_premium=250.0, stop_loss_pct=0.40, confidence=0.25)
            self.assertEqual(lots, 8)

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

    def test_budget_sizing_scales_within_hard_notional_cap(self) -> None:
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

            floored = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.40, confidence=0.10)
            floor = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.40, confidence=0.65)
            higher = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.40, confidence=0.80)
            full = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.40, confidence=1.00)

            self.assertEqual(floored, floor)
            self.assertEqual(floor, 21)
            self.assertGreater(higher, floor)
            self.assertEqual(higher, 26)
            self.assertEqual(full, 33)

    def test_risk_based_sizing_scales_within_hard_risk_cap(self) -> None:
        mgr = RiskManager()
        mgr.on_session_start(date(2026, 3, 5))

        floor = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.20, confidence=0.65)
        higher = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.20, confidence=0.80)
        full = mgr.compute_lots(entry_premium=200.0, stop_loss_pct=0.20, confidence=1.0)

        self.assertEqual(floor, 2)
        self.assertEqual(higher, 3)
        self.assertEqual(full, 4)

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

    def test_session_trade_cap_triggers_kill_switch(self) -> None:
        with mock.patch.dict("os.environ", {"RISK_MAX_SESSION_TRADES": "2"}, clear=False):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            mgr.record_trade_result(pnl_pct=0.10, lots=1, entry_premium=100.0)
            self.assertFalse(mgr.is_halted)

            mgr.record_trade_result(pnl_pct=-0.10, lots=1, entry_premium=100.0)

            self.assertTrue(mgr.is_halted)
            self.assertEqual(mgr.halt_reason, "session_trade_cap")
            self.assertEqual(mgr.context.session_trade_count, 2)
            self.assertTrue(mgr.context.session_trade_cap_breached)

    def test_consecutive_losses_expose_pause_reason(self) -> None:
        with mock.patch.dict("os.environ", {"RISK_MAX_CONSECUTIVE_LOSSES": "2"}, clear=False):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            mgr.record_trade_result(pnl_pct=-0.10, lots=1, entry_premium=100.0)
            mgr.record_trade_result(pnl_pct=-0.10, lots=1, entry_premium=100.0)
            mgr.update(_snap(ts="2026-03-05T10:00:00+05:30", vix_intraday_chg=0.0), None)

            self.assertTrue(mgr.is_paused)
            self.assertEqual(mgr.pause_reason, "consecutive_loss_pause")


if __name__ == "__main__":
    unittest.main()
