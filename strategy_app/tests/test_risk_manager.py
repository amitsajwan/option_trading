import os
import unittest
from tempfile import TemporaryDirectory
from datetime import date
from unittest import mock

from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.risk.manager import RiskManager


def _clear_ambient_risk_env() -> None:
    """Remove ambient RISK_* overrides (e.g. from a developer .env loaded into the
    shell) so profile-preset tests exercise the preset, not the local environment.
    Call inside a mock.patch.dict(os.environ, ...) block — patch.dict restores all
    keys on exit. RISK_PROFILE is preserved so the profile still loads."""
    for k in [k for k in list(os.environ) if k.startswith("RISK_") and k != "RISK_PROFILE"]:
        os.environ.pop(k, None)


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
    def test_operator_halt_sentinel_triggers_kill_switch(self) -> None:
        with TemporaryDirectory() as tmp_dir, mock.patch.dict("os.environ", {"STRATEGY_RUN_DIR": tmp_dir}, clear=False):
            mgr = RiskManager()
            self.assertFalse(mgr.is_halted)

            halt_path = mgr._operator_halt_path
            halt_path.parent.mkdir(parents=True, exist_ok=True)
            halt_path.touch()

            self.assertTrue(mgr.is_halted)
            self.assertEqual(mgr.halt_reason, "operator_halt")

    def test_budget_per_trade_sizing_uses_notional_budget(self) -> None:
        with mock.patch("strategy_app.constants.BANKNIFTY_LOT_SIZE", 15), mock.patch.dict(
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
        with mock.patch("strategy_app.constants.BANKNIFTY_LOT_SIZE", 15), mock.patch.dict(
            "os.environ",
            {"RISK_PROFILE": "aggressive_safe_v1"},
            clear=False,
        ):
            _clear_ambient_risk_env()
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            self.assertEqual(mgr.context.max_consecutive_losses, 3)
            self.assertAlmostEqual(float(mgr.context.max_daily_loss_pct), 0.02, places=8)
            self.assertEqual(mgr.context.max_lots_per_trade, 20)
            # The profile keeps the configured notional cap and scales down at the floor:
            # int(50000 / (250 * 15)) = 13 lots, scaled -> int(13 * 0.65) = 8.
            lots = mgr.compute_lots(entry_premium=250.0, stop_loss_pct=0.40, confidence=0.25)
            self.assertEqual(lots, 8)

    def test_live_eligible_good_grade_clean_session(self) -> None:
        with TemporaryDirectory() as tmp_dir, mock.patch.dict(
            "os.environ", {"STRATEGY_RUN_DIR": tmp_dir}, clear=False
        ):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 6, 3))
            ok, reason = mgr.live_eligible(grade="GOOD", confidence=0.9)
            self.assertTrue(ok, reason)

    def test_live_eligible_bad_grade_blocked(self) -> None:
        with TemporaryDirectory() as tmp_dir, mock.patch.dict(
            "os.environ", {"STRATEGY_RUN_DIR": tmp_dir}, clear=False
        ):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 6, 3))
            ok, reason = mgr.live_eligible(grade="BAD", confidence=0.9)
            self.assertFalse(ok)
            self.assertIn("grade_below_min", reason)

    def test_live_eligible_blocked_when_operator_halted(self) -> None:
        with TemporaryDirectory() as tmp_dir, mock.patch.dict(
            "os.environ", {"STRATEGY_RUN_DIR": tmp_dir}, clear=False
        ):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 6, 3))
            halt_path = mgr._operator_halt_path
            halt_path.parent.mkdir(parents=True, exist_ok=True)
            halt_path.touch()
            ok, reason = mgr.live_eligible(grade="GOOD", confidence=0.9)
            self.assertFalse(ok)
            self.assertIn("operator_halt", reason)

    def test_live_eligible_confidence_floor(self) -> None:
        with TemporaryDirectory() as tmp_dir, mock.patch.dict(
            "os.environ", {"STRATEGY_RUN_DIR": tmp_dir, "RISK_CONFIDENCE_FLOOR": "0.65"}, clear=False
        ):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 6, 3))
            ok, reason = mgr.live_eligible(grade="GOOD", confidence=0.50)
            self.assertFalse(ok)
            self.assertIn("confidence_below_floor", reason)

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
        with mock.patch("strategy_app.constants.BANKNIFTY_LOT_SIZE", 15), mock.patch.dict(
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
        # Explicit capital/lot_size to be independent of contracts_app/.env auto-load.
        # capital=500k, risk=0.5%, lot_size=15, entry=200, stop=20%
        # risk_capital=2500, mllpl=600, base_lots=4
        # floor (0.65): int(4*0.65)=2; higher (0.80): int(4*0.80)=3; full (1.0): 4
        with mock.patch("strategy_app.constants.BANKNIFTY_LOT_SIZE", 15), mock.patch.dict(
            "os.environ",
            {
                "RISK_CAPITAL_ALLOCATED": "500000",
                "RISK_PER_TRADE_PCT": "0.005",
                "RISK_MAX_LOTS_PER_TRADE": "20",
            },
            clear=False,
        ):
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

    def test_consecutive_losses_disabled_when_limit_zero(self) -> None:
        # Paper trading: RISK_MAX_CONSECUTIVE_LOSSES=0 means unlimited — never pause.
        with mock.patch.dict("os.environ", {"RISK_MAX_CONSECUTIVE_LOSSES": "0"}, clear=False):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            for _ in range(6):
                mgr.record_trade_result(pnl_pct=-0.10, lots=1, entry_premium=100.0)
            mgr.update(_snap(ts="2026-03-05T10:00:00+05:30", vix_intraday_chg=0.0), None)
            self.assertFalse(mgr.is_paused)
            self.assertIsNone(mgr.pause_reason)

    def test_session_cap_disabled_when_zero(self) -> None:
        with mock.patch.dict("os.environ", {"RISK_MAX_SESSION_TRADES": "0"}, clear=False):
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            for _ in range(10):
                mgr.record_trade_result(pnl_pct=0.05, lots=1, entry_premium=100.0)
            mgr.update(_snap(ts="2026-03-05T10:00:00+05:30", vix_intraday_chg=0.0), None)
            self.assertFalse(mgr.context.session_trade_cap_breached)
            self.assertFalse(mgr.is_halted)


class RiskManagerCapitalPoolTests(unittest.TestCase):
    """CAPITAL_POOL_ENABLED defaults off (2026-09-21) -- the core guarantee
    under test here is that with the flag off, RiskManager NEVER touches
    contracts_app.capital_pool at all, so behavior is provably byte-for-byte
    identical to before this feature existed."""

    def test_disabled_by_default_never_touches_capital_pool_module(self) -> None:
        with mock.patch("strategy_app.risk.manager.capital_pool") as mock_pool:
            mgr = RiskManager()
            ok, reason = mgr.try_reserve_capital(lots=2, entry_premium=100.0)
            self.assertTrue(ok)
            self.assertEqual(reason, "disabled")
            mgr.release_capital_reservation()  # must also be a safe no-op
            mock_pool.try_reserve.assert_not_called()
            mock_pool.release.assert_not_called()
            mock_pool.ensure_pool_document.assert_not_called()

    def test_sim_bypass_never_touches_capital_pool_even_when_enabled(self) -> None:
        with mock.patch.dict("os.environ", {"CAPITAL_POOL_ENABLED": "1"}, clear=False), mock.patch(
            "strategy_app.risk.manager.capital_pool"
        ) as mock_pool:
            mgr = RiskManager()
            ok, reason = mgr.try_reserve_capital(lots=2, entry_premium=100.0, bypass=True)
            self.assertTrue(ok)
            self.assertEqual(reason, "sim_bypass")
            mock_pool.try_reserve.assert_not_called()

    def test_enabled_and_insufficient_pool_capital_blocks_the_entry(self) -> None:
        with mock.patch.dict("os.environ", {"CAPITAL_POOL_ENABLED": "1"}, clear=False), mock.patch(
            "strategy_app.risk.manager.capital_pool"
        ) as mock_pool:
            mock_pool.try_reserve.return_value = (False, "insufficient_pool_capital")
            mgr = RiskManager()
            ok, reason = mgr.try_reserve_capital(lots=5, entry_premium=200.0)
            self.assertFalse(ok)
            self.assertEqual(reason, "insufficient_pool_capital")
            mock_pool.try_reserve.assert_called_once()

    def test_enabled_and_successful_reserve_then_record_trade_result_releases_it(self) -> None:
        with mock.patch.dict("os.environ", {"CAPITAL_POOL_ENABLED": "1"}, clear=False), mock.patch(
            "strategy_app.risk.manager.capital_pool"
        ) as mock_pool:
            mock_pool.try_reserve.return_value = (True, "reserved")
            mgr = RiskManager()
            mgr.on_session_start(date(2026, 3, 5))
            ok, _ = mgr.try_reserve_capital(lots=3, entry_premium=150.0)
            self.assertTrue(ok)
            mock_pool.release.assert_not_called()

            mgr.record_trade_result(pnl_pct=0.05, lots=3, entry_premium=150.0)
            mock_pool.release.assert_called_once()
            # Amount released must match what was actually reserved (lots * premium * lot_size),
            # not something recomputed from the (possibly different) closing trade args.
            released_amount = mock_pool.release.call_args.args[1]
            self.assertGreater(released_amount, 0)

    def test_release_is_idempotent(self) -> None:
        with mock.patch.dict("os.environ", {"CAPITAL_POOL_ENABLED": "1"}, clear=False), mock.patch(
            "strategy_app.risk.manager.capital_pool"
        ) as mock_pool:
            mock_pool.try_reserve.return_value = (True, "reserved")
            mgr = RiskManager()
            mgr.try_reserve_capital(lots=1, entry_premium=100.0)
            mgr.release_capital_reservation()
            mgr.release_capital_reservation()  # second call: nothing left to release
            self.assertEqual(mock_pool.release.call_count, 1)


if __name__ == "__main__":
    unittest.main()
