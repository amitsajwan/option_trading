"""Unit tests for exit policy stack (E2-S1 through E2-S4 DoD)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from strategy_app.contracts import ExitReason, PositionContext
from strategy_app.position.exit_policy import (
    CompositeExitPolicy,
    GivebackStopPolicy,
    PremiumTargetPolicy,
    ThesisFailPolicy,
    TrailingStopPolicy,
    HardStopPolicy,
    BigTargetPolicy,
    RunnerTrailPolicy,
    MomentumReversalPolicy,
    ExpiryAwareExitPolicy,
    build_default_exit_stack,
    build_lottery_exit_stack,
    build_adaptive_exit_stack,
    build_expiry_exit_stack,
    build_scalper_exit_stack,
)


def _pos(**kwargs) -> PositionContext:
    defaults = dict(
        position_id="test-pos",
        direction="PE",
        strike=54200,
        expiry=None,
        entry_premium=1000.0,
        entry_time=datetime.now(),
        entry_snapshot_id="snap",
        lots=1,
        pnl_pct=0.0,
        mfe_pct=0.0,
        mae_pct=0.0,
        bars_held=0,
    )
    defaults.update(kwargs)
    return PositionContext(**defaults)


_snap = MagicMock()


class TestPremiumTargetPolicy:
    def test_fires_when_target_reached(self):
        policy = PremiumTargetPolicy(target_pct=0.015)
        pos = _pos(pnl_pct=0.02)
        assert policy.check(pos, _snap) == ExitReason.TARGET_HIT

    def test_fires_at_exact_target(self):
        policy = PremiumTargetPolicy(target_pct=0.015)
        pos = _pos(pnl_pct=0.015)
        assert policy.check(pos, _snap) == ExitReason.TARGET_HIT

    def test_silent_below_target(self):
        policy = PremiumTargetPolicy(target_pct=0.015)
        pos = _pos(pnl_pct=0.01)
        assert policy.check(pos, _snap) is None

    def test_silent_when_negative(self):
        policy = PremiumTargetPolicy(target_pct=0.015)
        pos = _pos(pnl_pct=-0.05)
        assert policy.check(pos, _snap) is None


class TestTrailingStopPolicy:
    def test_fires_when_trail_breached(self):
        # MFE=1.5%, pnl=0.9%, trail=0.5% → 0.9 < 1.5 - 0.5 = 1.0 → fire
        policy = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)
        pos = _pos(mfe_pct=0.015, pnl_pct=0.009)
        assert policy.check(pos, _snap) == ExitReason.TRAILING_STOP

    def test_silent_below_activation(self):
        # MFE=0.8% < activation=1% → not yet active
        policy = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)
        pos = _pos(mfe_pct=0.008, pnl_pct=0.001)
        assert policy.check(pos, _snap) is None

    def test_silent_within_trail(self):
        # MFE=2%, pnl=1.6%, trail=0.5% → 1.6 > 2.0 - 0.5 = 1.5 → no fire
        policy = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)
        pos = _pos(mfe_pct=0.02, pnl_pct=0.016)
        assert policy.check(pos, _snap) is None

    def test_trail_from_doc_example(self):
        # Trade 2: MFE=1.14%, pnl=-0.64% → 1.14 - 0.5 = 0.64 → pnl < 0.64 → fire
        policy = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)
        pos = _pos(mfe_pct=0.0114, pnl_pct=-0.0064)
        assert policy.check(pos, _snap) == ExitReason.TRAILING_STOP


class TestThesisFailPolicy:
    def test_fires_when_no_mfe_after_bars(self):
        policy = ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002)
        pos = _pos(bars_held=3, mfe_pct=0.0)
        assert policy.check(pos, _snap) == ExitReason.THESIS_FAIL

    def test_silent_below_min_bars(self):
        policy = ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002)
        pos = _pos(bars_held=2, mfe_pct=0.0)
        assert policy.check(pos, _snap) is None

    def test_silent_when_mfe_sufficient(self):
        policy = ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002)
        pos = _pos(bars_held=3, mfe_pct=0.003)
        assert policy.check(pos, _snap) is None

    def test_fires_after_many_bars_with_zero_mfe(self):
        # CE trades 3,4,5 from 2026-06-01: MFE=0%
        policy = ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002)
        pos = _pos(bars_held=5, mfe_pct=0.0, pnl_pct=-0.015)
        assert policy.check(pos, _snap) == ExitReason.THESIS_FAIL


class TestCompositeExitPolicy:
    def test_first_trigger_wins(self):
        p1 = PremiumTargetPolicy(target_pct=0.05)  # won't fire (pnl=0.02)
        p2 = TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)  # will fire
        composite = CompositeExitPolicy([p1, p2])
        pos = _pos(mfe_pct=0.015, pnl_pct=0.009)
        assert composite.check(pos, _snap) == ExitReason.TRAILING_STOP

    def test_none_when_no_policy_fires(self):
        composite = CompositeExitPolicy([
            PremiumTargetPolicy(0.05),
            TrailingStopPolicy(0.05, 0.01),
            ThesisFailPolicy(10, 0.002),
        ])
        pos = _pos(pnl_pct=0.01, mfe_pct=0.01, bars_held=1)
        assert composite.check(pos, _snap) is None

    def test_name_is_descriptive(self):
        composite = CompositeExitPolicy([PremiumTargetPolicy(0.015)])
        assert "premium_target" in composite.name


class TestBuildDefaultExitStack:
    def test_builds_without_env(self, monkeypatch):
        monkeypatch.delenv("EXIT_PREMIUM_TARGET_PCT", raising=False)
        monkeypatch.delenv("EXIT_TRAILING_ACTIVATION_PCT", raising=False)
        monkeypatch.delenv("EXIT_TRAILING_TRAIL_PCT", raising=False)
        monkeypatch.delenv("EXIT_THESIS_FAIL_BARS", raising=False)
        monkeypatch.delenv("EXIT_THESIS_FAIL_MIN_MFE", raising=False)
        stack = build_default_exit_stack()
        assert stack is not None
        assert "composite" in stack.name

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("EXIT_PREMIUM_TARGET_PCT", "0.03")
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "scalper")
        stack = build_default_exit_stack()
        assert "3.0%" in stack.name


class TestUniversalMaxLossFloor:
    """The 2026-06-05 footgun: adaptive mode routed TRENDING/BREAKOUT trades to the
    lottery 20% stop, so EXIT_SCALPER_HARD_STOP_PCT=0.05 protected nothing and a
    trade ran to -13%. The universal EXIT_MAX_LOSS_PCT floor must catch it."""

    def test_floor_catches_trending_trade_in_adaptive_mode(self, monkeypatch):
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "adaptive")
        monkeypatch.setenv("EXIT_MAX_LOSS_PCT", "0.10")
        monkeypatch.setenv("LOTTERY_HARD_STOP_PCT", "0.20")  # the loose stop that failed
        monkeypatch.setenv("EXIT_SCALPER_HARD_STOP_PCT", "0.05")  # only protected scalper
        stack = build_default_exit_stack()
        # TRENDING -> routes to lottery (20% stop). At -12% the lottery stop is silent,
        # but the universal 10% floor must fire.
        assert stack.check(_pos(pnl_pct=-0.12, entry_regime="TRENDING"), _snap) == ExitReason.STOP_LOSS
        assert "hard_stop_10%" in stack.name

    def test_floor_default_on_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("EXIT_MAX_LOSS_PCT", raising=False)
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "adaptive")
        stack = build_default_exit_stack()
        assert stack.check(_pos(pnl_pct=-0.15, entry_regime="BREAKOUT"), _snap) == ExitReason.STOP_LOSS

    def test_floor_disabled_at_one(self, monkeypatch):
        monkeypatch.setenv("EXIT_MAX_LOSS_PCT", "1.0")
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "scalper")
        monkeypatch.setenv("EXIT_SCALPER_HARD_STOP_PCT", "0.25")
        stack = build_default_exit_stack()
        # floor off -> only the scalper 25% stop; -12% is silent
        assert stack.check(_pos(pnl_pct=-0.12, entry_regime="SIDEWAYS"), _snap) is None


class TestHardStopPolicy:
    def test_fires_at_stop(self):
        p = HardStopPolicy(0.25)
        assert p.check(_pos(pnl_pct=-0.25), _snap) == ExitReason.STOP_LOSS
        assert p.check(_pos(pnl_pct=-0.30), _snap) == ExitReason.STOP_LOSS

    def test_silent_above_stop(self):
        p = HardStopPolicy(0.25)
        assert p.check(_pos(pnl_pct=-0.10), _snap) is None

    def test_disabled_at_one(self):
        p = HardStopPolicy(1.0)  # disabled — ride to zero
        assert p.check(_pos(pnl_pct=-0.95), _snap) is None
        assert "off" in p.name


class TestBigTargetPolicy:
    def test_fires_at_big_target(self):
        p = BigTargetPolicy(0.40)
        assert p.check(_pos(pnl_pct=0.40), _snap) == ExitReason.TARGET_HIT
        assert p.check(_pos(pnl_pct=0.55), _snap) == ExitReason.TARGET_HIT

    def test_silent_below(self):
        # Crucially: does NOT exit at +2% like the scalper target would
        p = BigTargetPolicy(0.40)
        assert p.check(_pos(pnl_pct=0.02), _snap) is None
        assert p.check(_pos(pnl_pct=0.24), _snap) is None


class TestRunnerTrailPolicy:
    def test_silent_before_activation(self):
        # MFE only +15%, activation +20% → not active, let it run
        p = RunnerTrailPolicy(activation_mfe=0.20, giveback_frac=0.40)
        assert p.check(_pos(mfe_pct=0.15, pnl_pct=0.05), _snap) is None

    def test_lets_winner_run(self):
        # MFE +30%, floor = 30*(1-0.4)=18%; pnl 25% > 18% → hold, let it run
        p = RunnerTrailPolicy(activation_mfe=0.20, giveback_frac=0.40)
        assert p.check(_pos(mfe_pct=0.30, pnl_pct=0.25), _snap) is None

    def test_protects_fat_winner(self):
        # MFE +30%, floor 18%; pnl fell to 15% < 18% → lock it in
        p = RunnerTrailPolicy(activation_mfe=0.20, giveback_frac=0.40)
        assert p.check(_pos(mfe_pct=0.30, pnl_pct=0.15), _snap) == ExitReason.TRAILING_STOP


class TestGivebackStopPolicy:
    def test_silent_before_min_mfe_activation(self):
        # MFE=2% < activation=3% → policy stays silent regardless of pnl
        p = GivebackStopPolicy(min_mfe=0.03, giveback_pct=0.09)
        assert p.check(_pos(mfe_pct=0.02, pnl_pct=-0.10), _snap) is None

    def test_fires_on_jun4_dead_zone_pattern(self):
        # Jun 4: MFE=+4%, pnl ground to -18%. Floor = 4% - 9% = -5%.
        # Should fire at -6% (below -5% floor).
        p = GivebackStopPolicy(min_mfe=0.03, giveback_pct=0.09)
        assert p.check(_pos(mfe_pct=0.04, pnl_pct=-0.06), _snap) == ExitReason.TRAILING_STOP

    def test_fires_exactly_at_floor(self):
        # MFE=5%, giveback=9% → floor=-4%. pnl exactly at -4% → fires.
        p = GivebackStopPolicy(min_mfe=0.03, giveback_pct=0.09)
        assert p.check(_pos(mfe_pct=0.05, pnl_pct=-0.04), _snap) == ExitReason.TRAILING_STOP

    def test_silent_above_floor(self):
        # MFE=5%, giveback=9% → floor=-4%. pnl=-3% (above floor) → holds.
        p = GivebackStopPolicy(min_mfe=0.03, giveback_pct=0.09)
        assert p.check(_pos(mfe_pct=0.05, pnl_pct=-0.03), _snap) is None

    def test_floor_rises_with_mfe(self):
        # MFE=6%, giveback=9% → floor=-3%. Original entry at MFE=4% had floor=-5%.
        # The floor is dynamic — it tightens as MFE grows.
        p = GivebackStopPolicy(min_mfe=0.03, giveback_pct=0.09)
        assert p.check(_pos(mfe_pct=0.06, pnl_pct=-0.04), _snap) == ExitReason.TRAILING_STOP
        assert p.check(_pos(mfe_pct=0.06, pnl_pct=-0.02), _snap) is None

    def test_wider_lottery_giveback(self):
        # Lottery uses 15% giveback so genuine winners aren't choked.
        # MFE=5%, giveback=15% → floor=-10%; pnl=-8% → still holds.
        p = GivebackStopPolicy(min_mfe=0.03, giveback_pct=0.15)
        assert p.check(_pos(mfe_pct=0.05, pnl_pct=-0.08), _snap) is None
        assert p.check(_pos(mfe_pct=0.05, pnl_pct=-0.11), _snap) == ExitReason.TRAILING_STOP

    def test_name_is_descriptive(self):
        p = GivebackStopPolicy(min_mfe=0.03, giveback_pct=0.09)
        assert "giveback_act=3%" in p.name
        assert "give=9%" in p.name


class TestGivebackStopStackWiring:
    """GivebackStopPolicy wired into scalper and lottery stacks via env vars."""

    def test_disabled_by_default_scalper(self, monkeypatch):
        monkeypatch.delenv("EXIT_GIVEBACK_STOP_ENABLED", raising=False)
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "scalper")
        stack = build_default_exit_stack()
        assert "giveback_act" not in stack.name

    def test_enabled_appears_in_scalper_stack(self, monkeypatch):
        monkeypatch.setenv("EXIT_GIVEBACK_STOP_ENABLED", "1")
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "scalper")
        stack = build_default_exit_stack()
        assert "giveback_act" in stack.name

    def test_enabled_appears_in_lottery_stack(self, monkeypatch):
        monkeypatch.setenv("EXIT_GIVEBACK_STOP_ENABLED", "1")
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "lottery")
        stack = build_default_exit_stack()
        assert "giveback_act" in stack.name

    def test_scalper_giveback_fires_on_dead_zone(self, monkeypatch):
        # Scalper stack enabled with giveback. MFE=4%, pnl=-7% → floor=-5% → fires.
        monkeypatch.setenv("EXIT_GIVEBACK_STOP_ENABLED", "1")
        monkeypatch.setenv("EXIT_GIVEBACK_MIN_MFE", "0.03")
        monkeypatch.setenv("EXIT_GIVEBACK_PCT", "0.09")
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "scalper")
        monkeypatch.setenv("EXIT_MAX_LOSS_PCT", "1.0")  # disable universal floor
        stack = build_scalper_exit_stack()
        assert stack.check(_pos(mfe_pct=0.04, pnl_pct=-0.07), _snap) == ExitReason.TRAILING_STOP

    def test_scalper_giveback_silent_before_activation(self, monkeypatch):
        monkeypatch.setenv("EXIT_GIVEBACK_STOP_ENABLED", "1")
        monkeypatch.setenv("EXIT_GIVEBACK_MIN_MFE", "0.03")
        monkeypatch.setenv("EXIT_GIVEBACK_PCT", "0.09")
        monkeypatch.setenv("EXIT_TRAILING_ACTIVATION_PCT", "0.10")  # raise trail so only giveback matters
        monkeypatch.setenv("EXIT_SCALPER_HARD_STOP_PCT", "1.0")     # disable hard stop
        stack = build_scalper_exit_stack()
        # MFE=2% < giveback activation=3% → giveback stays silent; trade at -5% not fired
        assert stack.check(_pos(mfe_pct=0.02, pnl_pct=-0.05), _snap) is None

    def test_lottery_uses_wider_giveback(self, monkeypatch):
        monkeypatch.setenv("EXIT_GIVEBACK_STOP_ENABLED", "1")
        monkeypatch.setenv("EXIT_GIVEBACK_MIN_MFE", "0.03")
        monkeypatch.setenv("LOTTERY_GIVEBACK_PCT", "0.15")
        stack = build_lottery_exit_stack()
        # MFE=5%, pnl=-8% < -10% floor? No, -8% > floor(-5%+...wait: 5%-15%=-10%)
        # So -8% > -10% → holds (lottery wider tolerance)
        assert stack.check(_pos(mfe_pct=0.05, pnl_pct=-0.08), _snap) is None
        # -11% < -10% → fires
        assert stack.check(_pos(mfe_pct=0.05, pnl_pct=-0.11), _snap) == ExitReason.TRAILING_STOP

    def test_env_override_min_mfe(self, monkeypatch):
        monkeypatch.setenv("EXIT_GIVEBACK_STOP_ENABLED", "1")
        monkeypatch.setenv("EXIT_GIVEBACK_MIN_MFE", "0.05")
        monkeypatch.setenv("EXIT_GIVEBACK_PCT", "0.09")
        monkeypatch.setenv("EXIT_TRAILING_ACTIVATION_PCT", "0.20")  # raise trail so only giveback matters
        monkeypatch.setenv("EXIT_SCALPER_HARD_STOP_PCT", "1.0")     # disable hard stop
        stack = build_scalper_exit_stack()
        # Activation threshold raised to 5%; MFE=4% < 5% → giveback silent
        assert stack.check(_pos(mfe_pct=0.04, pnl_pct=-0.07), _snap) is None
        # MFE=6% ≥ 5% → active; floor=6%-9%=-3%; pnl=-4% → fires
        assert stack.check(_pos(mfe_pct=0.06, pnl_pct=-0.04), _snap) == ExitReason.TRAILING_STOP


class TestMomentumReversalPolicy:
    def test_pe_exits_on_bullish_flip(self):
        p = MomentumReversalPolicy(1.0)
        assert p.check(_pos(direction="PE", current_shadow_score=1.5), _snap) == ExitReason.REGIME_SHIFT

    def test_pe_holds_when_aligned(self):
        p = MomentumReversalPolicy(1.0)
        assert p.check(_pos(direction="PE", current_shadow_score=-2.0), _snap) is None

    def test_ce_exits_on_bearish_flip(self):
        p = MomentumReversalPolicy(1.0)
        assert p.check(_pos(direction="CE", current_shadow_score=-1.5), _snap) == ExitReason.REGIME_SHIFT


class TestLotteryStack:
    def test_builds_lottery_mode(self, monkeypatch):
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "lottery")
        stack = build_default_exit_stack()
        assert "hard_stop" in stack.name
        assert "big_target" in stack.name
        assert "runner_trail" in stack.name
        # Scalper-only policies must NOT be in lottery mode
        assert "premium_target" not in stack.name   # scalper emergency target
        assert "give=" in stack.name                # lottery runner giveback

    def test_lottery_lets_24pct_run(self):
        # The real 2026-06-01 case: trade reached +24%. Scalper exits at +0.5%.
        # Lottery should still be holding at +20% (target 40%, runner floor not breached).
        stack = build_lottery_exit_stack()
        pos = _pos(pnl_pct=0.20, mfe_pct=0.20, bars_held=5)
        # MFE 20% just hit activation; floor = 20*0.6 = 12%; pnl 20% > 12% → hold
        assert stack.check(pos, _snap) is None


class TestScalperHardStop:
    """Scalper sub-stack must carry its own HardStop so the adaptive stack is
    self-sufficient for loss protection when legacy inline exits are suppressed."""

    def test_scalper_has_hard_stop_by_default(self, monkeypatch):
        monkeypatch.delenv("EXIT_SCALPER_HARD_STOP_PCT", raising=False)
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "scalper")
        stack = build_default_exit_stack()
        assert "hard_stop_25%" in stack.name

    def test_scalper_hard_stop_fires_at_cap(self, monkeypatch):
        monkeypatch.setenv("EXIT_SCALPER_HARD_STOP_PCT", "0.25")
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "scalper")
        stack = build_default_exit_stack()
        # -30% loss exceeds the 25% cap → HardStop must fire (loss protection
        # no longer depends on the tracker's legacy inline stop-losses).
        pos = _pos(pnl_pct=-0.30, mfe_pct=0.0, bars_held=2)
        assert stack.check(pos, _snap) == ExitReason.STOP_LOSS

    def test_scalper_hard_stop_disabled_when_ge_one(self, monkeypatch):
        monkeypatch.setenv("EXIT_SCALPER_HARD_STOP_PCT", "1.0")
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "scalper")
        stack = build_default_exit_stack()
        assert "hard_stop_off" in stack.name


class TestAdaptiveStack:
    def test_builds_adaptive_mode(self, monkeypatch):
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "adaptive")
        stack = build_default_exit_stack()
        assert "adaptive" in stack.name
        assert "lottery=BREAKOUT,TRENDING" in stack.name

    def test_breakout_regime_uses_lottery(self):
        stack = build_adaptive_exit_stack()
        # BREAKOUT entry: MFE hit 5%, pnl pulled back to 0.5%.
        # Scalper trail (act=1%, trail=0.5%): floor = 5%-0.5% = 4.5%; pnl 0.5% < 4.5% → STOP
        # Lottery runner (act=20% default): mfe 5% < 20% → not active; thesis_fail min_mfe=3%
        #   satisfied (5%>3%); no other exit fires → holds
        pos = _pos(entry_regime="BREAKOUT", pnl_pct=0.005, mfe_pct=0.05, bars_held=5)
        assert stack.check(pos, _snap) is None  # lottery lets it run

    def test_sideways_regime_uses_scalper(self, monkeypatch):
        monkeypatch.setenv("EXIT_THESIS_FAIL_BARS", "3")
        monkeypatch.setenv("EXIT_THESIS_FAIL_MIN_MFE", "0.002")
        stack = build_adaptive_exit_stack()
        # SIDEWAYS entry, 3 bars held, MFE never moved → scalper thesis_fail fires
        pos = _pos(entry_regime="SIDEWAYS", pnl_pct=-0.01, mfe_pct=0.0, bars_held=3)
        assert stack.check(pos, _snap) == ExitReason.THESIS_FAIL

    def test_trending_regime_uses_lottery(self):
        stack = build_adaptive_exit_stack()
        # TRENDING entry, +30% MFE, pnl still +25% — lottery holds (target 50% not hit)
        pos = _pos(entry_regime="TRENDING", pnl_pct=0.25, mfe_pct=0.30, bars_held=15)
        assert stack.check(pos, _snap) is None

    def test_custom_lottery_regimes(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_LOTTERY_REGIMES", "HIGH_VOL")
        stack = build_adaptive_exit_stack()
        assert "lottery=HIGH_VOL" in stack.name
        # HIGH_VOL → lottery (runs)
        pos_hv = _pos(entry_regime="HIGH_VOL", pnl_pct=0.01, mfe_pct=0.10, bars_held=5)
        # BREAKOUT → scalper (thesis_fail fires at 3b with zero MFE, but MFE=10% so no)
        pos_bo = _pos(entry_regime="BREAKOUT", pnl_pct=-0.01, mfe_pct=0.0, bars_held=3)
        assert stack.check(pos_hv, _snap) is None   # lottery holds
        assert stack.check(pos_bo, _snap) == ExitReason.THESIS_FAIL  # scalper cuts


def _snap_dte(is_expiry=False, dte=None):
    m = MagicMock()
    m.is_expiry_day = is_expiry
    m.days_to_expiry = dte
    return m


class TestExpiryAwareStack:
    def test_off_by_default_no_wrap(self, monkeypatch):
        monkeypatch.delenv("EXIT_EXPIRY_OVERRIDE_ENABLED", raising=False)
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "adaptive")
        stack = build_default_exit_stack()
        assert "expiry_aware" not in stack.name

    def test_enabled_wraps_stack(self, monkeypatch):
        monkeypatch.setenv("EXIT_EXPIRY_OVERRIDE_ENABLED", "1")
        monkeypatch.setenv("EXIT_STRATEGY_MODE", "adaptive")
        stack = build_default_exit_stack()
        assert "expiry_aware" in stack.name

    def test_routes_to_expiry_stack_on_expiry_day(self, monkeypatch):
        # expiry hard stop 15% fires at -16%; normal scalper hard stop 25% does not.
        monkeypatch.setenv("EXIT_SCALPER_HARD_STOP_PCT", "0.25")
        monkeypatch.setenv("EXIT_EXPIRY_HARD_STOP_PCT", "0.15")
        wrapped = ExpiryAwareExitPolicy(
            normal=build_scalper_exit_stack(),
            expiry=build_expiry_exit_stack(),
            dte_threshold=0,
        )
        pos = _pos(pnl_pct=-0.16, mfe_pct=0.0, bars_held=1)
        assert wrapped.check(pos, _snap_dte(is_expiry=True)) == ExitReason.STOP_LOSS
        assert wrapped.check(pos, _snap_dte(is_expiry=False)) is None

    def test_dte_threshold_routing(self, monkeypatch):
        monkeypatch.setenv("EXIT_SCALPER_HARD_STOP_PCT", "0.25")
        monkeypatch.setenv("EXIT_EXPIRY_HARD_STOP_PCT", "0.15")
        wrapped = ExpiryAwareExitPolicy(
            normal=build_scalper_exit_stack(),
            expiry=build_expiry_exit_stack(),
            dte_threshold=1,
        )
        pos = _pos(pnl_pct=-0.16, mfe_pct=0.0, bars_held=1)
        assert wrapped.check(pos, _snap_dte(dte=1)) == ExitReason.STOP_LOSS  # within threshold
        assert wrapped.check(pos, _snap_dte(dte=3)) is None                 # far from expiry
        assert wrapped.check(pos, _snap_dte(dte=None)) is None              # unknown DTE → normal
