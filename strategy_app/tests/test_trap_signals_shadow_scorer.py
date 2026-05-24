"""E5-S1: Unit tests for failed-move trap signals in _shadow_direction_from_snapshot.

Tests verify that each of the 6 trap signals fires under the correct conditions
and contributes the expected sign to the shadow score.
"""
from __future__ import annotations

import unittest
from collections import deque

from strategy_app.contracts import Direction
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.market.snapshot_accessor import SnapshotAccessor


def _engine() -> DeterministicRuleEngine:
    """Minimal engine instance for shadow-scorer tests — no strategy router needed."""
    from unittest.mock import MagicMock
    return DeterministicRuleEngine(signal_logger=MagicMock())


def _snap(
    *,
    fut_close: float = 50000.0,
    price_vs_vwap: float = 0.0,
    atm_ce_iv: float = 20.0,
    atm_pe_iv: float = 20.0,
    orh: float = 50100.0,
    orl: float = 49900.0,
    orh_broken: bool = False,
    orl_broken: bool = False,
    vwap: float = 50000.0,
    fut_return_5m: float = 0.0,
    fut_return_15m: float = 0.0,
) -> SnapshotAccessor:
    """Build a minimal snapshot for shadow-scorer tests."""
    return SnapshotAccessor({
        "session_context": {
            "snapshot_id": "test-snap",
            "timestamp": "2024-08-01T10:00:00+05:30",
            "date": "2024-08-01",
            "minutes_since_open": 60,
        },
        "futures_bar": {"fut_close": fut_close},
        "futures_derived": {
            "price_vs_vwap": price_vs_vwap,
            "vwap": vwap,
            "return_5m": fut_return_5m,
            "return_15m": fut_return_15m,
        },
        "opening_range": {
            "orh": orh,
            "orl": orl,
            "orh_broken": orh_broken,
            "orl_broken": orl_broken,
        },
        "atm_ce_iv": atm_ce_iv,
        "atm_pe_iv": atm_pe_iv,
        "vix_context": {"vix_current": 12.0, "intraday_change_pct": 0.0},
    })


def _basis_signals(engine: DeterministicRuleEngine, snap: SnapshotAccessor) -> list[str]:
    """Extract the list of fired signal names from shadow scorer output."""
    _, basis, _ = engine._shadow_direction_from_snapshot(snap)
    # basis format: "multi_signal_ce(score=X:sig1,sig2,...)" or "tie_*(sigs)"
    import re
    m = re.search(r'\(score=[^:]*:([^)]+)\)', basis)
    if m:
        return m.group(1).split(',')
    m2 = re.search(r'\(([^)]+)\)', basis)
    if m2 and m2.group(1) != 'no_signals':
        return m2.group(1).split(',')
    return []


class TestORBTrapSignals(unittest.TestCase):
    def setUp(self) -> None:
        self.eng = _engine()

    def test_orb_low_rejected_fires_ce_signal(self) -> None:
        """Price broke ORB low earlier, now recovered above it → bullish trap signal present."""
        # orl=49900, price=49950 (above orl), orl_broken=True
        snap_rejected = _snap(orl=49900.0, orl_broken=True, fut_close=49950.0)
        snap_still_below = _snap(orl=49900.0, orl_broken=True, fut_close=49850.0)
        sigs = _basis_signals(self.eng, snap_rejected)
        self.assertIn('orb_low_rejected', sigs, f"Expected orb_low_rejected in {sigs}")
        # Score with rejection should be higher (more CE) than score without rejection
        _, _, score_rejected = self.eng._shadow_direction_from_snapshot(snap_rejected)
        _, _, score_not_rejected = self.eng._shadow_direction_from_snapshot(snap_still_below)
        self.assertGreater(score_rejected, score_not_rejected, "rejection should lift score vs non-rejection")

    def test_orb_low_rejected_not_when_price_still_below_orl(self) -> None:
        """Price is still below ORB low → not a rejection yet."""
        snap = _snap(orl=49900.0, orl_broken=True, fut_close=49850.0)
        sigs = _basis_signals(self.eng, snap)
        self.assertNotIn('orb_low_rejected', sigs)

    def test_orb_low_rejected_not_when_orl_not_broken(self) -> None:
        """ORB low never broke → no rejection signal."""
        snap = _snap(orl=49900.0, orl_broken=False, fut_close=49950.0)
        sigs = _basis_signals(self.eng, snap)
        self.assertNotIn('orb_low_rejected', sigs)

    def test_orb_high_rejected_fires_pe_signal(self) -> None:
        """Price broke ORB high earlier, now fell back below it → bearish trap signal present."""
        snap_rejected = _snap(orh=50100.0, orh_broken=True, fut_close=50050.0)
        snap_still_above = _snap(orh=50100.0, orh_broken=True, fut_close=50150.0)
        sigs = _basis_signals(self.eng, snap_rejected)
        self.assertIn('orb_high_rejected', sigs, f"Expected orb_high_rejected in {sigs}")
        # Score with rejection should be lower (more PE) than without rejection
        _, _, score_rejected = self.eng._shadow_direction_from_snapshot(snap_rejected)
        _, _, score_not_rejected = self.eng._shadow_direction_from_snapshot(snap_still_above)
        self.assertLess(score_rejected, score_not_rejected, "rejection should lower score vs non-rejection")

    def test_orb_high_rejected_not_when_price_still_above_orh(self) -> None:
        """Price is still above ORB high → not a rejection yet."""
        snap = _snap(orh=50100.0, orh_broken=True, fut_close=50150.0)
        sigs = _basis_signals(self.eng, snap)
        self.assertNotIn('orb_high_rejected', sigs)


class TestVWAPTrapSignals(unittest.TestCase):
    def setUp(self) -> None:
        self.eng = _engine()

    def test_vwap_reclaim_bull_fires_ce_signal(self) -> None:
        """Previous bar below VWAP, current bar above → bullish reclaim."""
        self.eng._pvwap_buf.extend([-0.5, 0.5])  # prev negative, current positive
        snap = _snap(price_vs_vwap=0.5)
        sigs = _basis_signals(self.eng, snap)
        self.assertIn('vwap_reclaim_bull', sigs, f"Expected vwap_reclaim_bull in {sigs}")

    def test_vwap_reclaim_bull_not_when_always_above(self) -> None:
        """Both bars above VWAP → not a reclaim."""
        self.eng._pvwap_buf.extend([0.3, 0.5])
        snap = _snap(price_vs_vwap=0.5)
        sigs = _basis_signals(self.eng, snap)
        self.assertNotIn('vwap_reclaim_bull', sigs)

    def test_vwap_reject_bear_fires_pe_signal(self) -> None:
        """Previous bar above VWAP, current bar below → bearish rejection."""
        self.eng._pvwap_buf.extend([0.5, -0.5])  # prev positive, current negative
        snap = _snap(price_vs_vwap=-0.5)
        sigs = _basis_signals(self.eng, snap)
        self.assertIn('vwap_reject_bear', sigs, f"Expected vwap_reject_bear in {sigs}")

    def test_vwap_reject_bear_not_when_always_below(self) -> None:
        """Both bars below VWAP → not a rejection."""
        self.eng._pvwap_buf.extend([-0.5, -0.3])
        snap = _snap(price_vs_vwap=-0.3)
        sigs = _basis_signals(self.eng, snap)
        self.assertNotIn('vwap_reject_bear', sigs)

    def test_vwap_signals_need_two_bars(self) -> None:
        """With only one bar in buffer, no VWAP trap fires."""
        self.eng._pvwap_buf.extend([-0.5])
        snap = _snap(price_vs_vwap=0.5)
        sigs = _basis_signals(self.eng, snap)
        self.assertNotIn('vwap_reclaim_bull', sigs)
        self.assertNotIn('vwap_reject_bear', sigs)


class TestIVFadeSignals(unittest.TestCase):
    def setUp(self) -> None:
        self.eng = _engine()

    def test_pe_iv_fading_fires_ce_signal(self) -> None:
        """PE IV spiked 2 bars ago (+6%), now compressing (-4%) → CE trap signal."""
        # pe_iv: 20 → 21.2 (+6%) → 20.35 (-4%)
        self.eng._iv_buf.extend([
            (20.0, 20.0),   # 3 bars ago: ce=20, pe=20
            (20.0, 21.2),   # 2 bars ago: pe spiked +6%
            (20.0, 20.35),  # current: pe compressed back -4%
        ])
        snap = _snap(atm_pe_iv=20.35)
        sigs = _basis_signals(self.eng, snap)
        self.assertIn('pe_iv_fading', sigs, f"Expected pe_iv_fading in {sigs}")

    def test_pe_iv_fading_not_when_spike_small(self) -> None:
        """PE IV spike < 5% → does not trigger."""
        self.eng._iv_buf.extend([
            (20.0, 20.0),
            (20.0, 20.9),  # only +4.5%, below 5% threshold
            (20.0, 20.0),
        ])
        snap = _snap(atm_pe_iv=20.0)
        sigs = _basis_signals(self.eng, snap)
        self.assertNotIn('pe_iv_fading', sigs)

    def test_pe_iv_fading_not_when_compression_small(self) -> None:
        """PE IV spiked but only compressed by 2% (need >3%) → does not trigger."""
        self.eng._iv_buf.extend([
            (20.0, 20.0),
            (20.0, 21.2),  # +6% spike
            (20.0, 20.98),  # only -1% compression — not enough
        ])
        snap = _snap(atm_pe_iv=20.98)
        sigs = _basis_signals(self.eng, snap)
        self.assertNotIn('pe_iv_fading', sigs)

    def test_ce_iv_fading_fires_pe_signal(self) -> None:
        """CE IV spiked 2 bars ago (+6%), now compressing (-4%) → PE trap signal."""
        self.eng._iv_buf.extend([
            (20.0, 20.0),   # 3 bars ago
            (21.2, 20.0),   # 2 bars ago: ce spiked +6%
            (20.35, 20.0),  # current: ce compressed -4%
        ])
        snap = _snap(atm_ce_iv=20.35)
        sigs = _basis_signals(self.eng, snap)
        self.assertIn('ce_iv_fading', sigs, f"Expected ce_iv_fading in {sigs}")

    def test_iv_signals_need_three_bars(self) -> None:
        """With only two bars in IV buffer, IV fade signals do not fire."""
        self.eng._iv_buf.extend([
            (20.0, 20.0),
            (21.2, 21.2),
        ])
        snap = _snap(atm_ce_iv=20.35, atm_pe_iv=20.35)
        sigs = _basis_signals(self.eng, snap)
        self.assertNotIn('pe_iv_fading', sigs)
        self.assertNotIn('ce_iv_fading', sigs)

    def test_iv_buffers_reset_on_session_start(self) -> None:
        """Buffers clear between sessions so stale IV from previous day doesn't bleed."""
        from datetime import date
        self.eng._iv_buf.extend([(20.0, 20.0), (21.2, 21.2), (20.35, 20.35)])
        self.eng._pvwap_buf.extend([-0.5, 0.5])
        self.eng.on_session_start(date(2024, 8, 1))
        self.assertEqual(len(self.eng._iv_buf), 0)
        self.assertEqual(len(self.eng._pvwap_buf), 0)


class TestTrapSignalDirectionality(unittest.TestCase):
    """Confirm each trap signal contributes to the correct trade direction."""

    def setUp(self) -> None:
        self.eng = _engine()

    def test_orb_low_rejected_is_ce_signal(self) -> None:
        """orb_low_rejected contributes positive score: rejected case > non-rejected case."""
        snap_rejected = _snap(orl=49900.0, orl_broken=True, fut_close=49960.0)
        snap_below = _snap(orl=49900.0, orl_broken=True, fut_close=49840.0)
        _, _, score_rej = self.eng._shadow_direction_from_snapshot(snap_rejected)
        _, _, score_below = self.eng._shadow_direction_from_snapshot(snap_below)
        self.assertGreater(score_rej, score_below, "rejection should add CE bias vs still-below")

    def test_orb_high_rejected_is_pe_signal(self) -> None:
        """orb_high_rejected contributes negative score: rejected case < non-rejected case."""
        snap_rejected = _snap(orh=50100.0, orh_broken=True, fut_close=50050.0)
        snap_above = _snap(orh=50100.0, orh_broken=True, fut_close=50150.0)
        _, _, score_rej = self.eng._shadow_direction_from_snapshot(snap_rejected)
        _, _, score_above = self.eng._shadow_direction_from_snapshot(snap_above)
        self.assertLess(score_rej, score_above, "rejection should add PE bias vs still-above")

    def test_pe_iv_fading_is_ce_signal(self) -> None:
        self.eng._iv_buf.extend([(20.0, 20.0), (20.0, 21.2), (20.0, 20.35)])
        snap = _snap()
        _, _, score = self.eng._shadow_direction_from_snapshot(snap)
        self.assertGreater(score, 0)

    def test_ce_iv_fading_is_pe_signal(self) -> None:
        self.eng._iv_buf.extend([(20.0, 20.0), (21.2, 20.0), (20.35, 20.0)])
        snap = _snap()
        _, _, score = self.eng._shadow_direction_from_snapshot(snap)
        self.assertLess(score, 0)


class TestDynamicStagnantExitPnLFloor(unittest.TestCase):
    """E4-S2 v2: shadow_score_crossed_zero must not hold underwater trades.

    Replay finding 2026-05-24: v1 held losing trades while shadow agreed with
    direction, producing TIME_STOP avg -6.5% (was +0.19%). Fix: only defer exit
    when pnl_pct > 0.
    """

    def _position(self, *, pnl_pct: float, direction: str, shadow_score: float,
                  bars: int = 15) -> object:
        from strategy_app.contracts import PositionContext
        from datetime import datetime, timezone
        return PositionContext(
            position_id="test",
            direction=direction,
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2024, 8, 1, 9, 30, tzinfo=timezone.utc),
            entry_snapshot_id="snap-001",
            lots=1,
            pnl_pct=pnl_pct,
            bars_held=bars,
            stagnant_exit_bars=12,
            stagnant_min_gain_pct=0.05,
            stagnant_exit_condition="shadow_score_crossed_zero",
            current_shadow_score=shadow_score,
        )

    def test_profitable_with_agreeing_shadow_is_held(self) -> None:
        """CE trade at +3% (below 5% threshold), shadow positive → hold."""
        from strategy_app.position.tracker import PositionTracker
        pos = self._position(pnl_pct=0.03, direction="CE", shadow_score=2.5)
        self.assertFalse(PositionTracker._is_stagnant_exit(pos))

    def test_losing_trade_exits_even_with_agreeing_shadow(self) -> None:
        """CE trade at -5% with still-positive shadow → exit (don't hold losers)."""
        from strategy_app.position.tracker import PositionTracker
        pos = self._position(pnl_pct=-0.05, direction="CE", shadow_score=2.5)
        self.assertTrue(PositionTracker._is_stagnant_exit(pos))

    def test_flat_trade_exits_even_with_agreeing_shadow(self) -> None:
        """CE trade at exactly 0% → exit (floor is strictly > 0)."""
        from strategy_app.position.tracker import PositionTracker
        pos = self._position(pnl_pct=0.0, direction="CE", shadow_score=1.0)
        self.assertTrue(PositionTracker._is_stagnant_exit(pos))

    def test_profitable_ce_reversed_shadow_exits(self) -> None:
        """CE trade at +3% but shadow has gone negative → momentum reversed, exit."""
        from strategy_app.position.tracker import PositionTracker
        pos = self._position(pnl_pct=0.03, direction="CE", shadow_score=-1.5)
        self.assertTrue(PositionTracker._is_stagnant_exit(pos))

    def test_profitable_pe_agreeing_shadow_is_held(self) -> None:
        """PE trade at +3%, shadow negative (PE-agreeing) → hold."""
        from strategy_app.position.tracker import PositionTracker
        pos = self._position(pnl_pct=0.03, direction="PE", shadow_score=-2.0)
        self.assertFalse(PositionTracker._is_stagnant_exit(pos))

    def test_losing_pe_exits_even_with_agreeing_shadow(self) -> None:
        """PE trade at -8%, shadow still negative (agreeing) → exit anyway."""
        from strategy_app.position.tracker import PositionTracker
        pos = self._position(pnl_pct=-0.08, direction="PE", shadow_score=-2.0)
        self.assertTrue(PositionTracker._is_stagnant_exit(pos))


if __name__ == "__main__":
    unittest.main()
