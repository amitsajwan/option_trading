"""Composable exit policies for position management.

Activate via EXIT_POLICY_STACK_ENABLED=1. Default stack:
  PremiumTargetPolicy -> TrailingStopPolicy -> ThesisFailPolicy

Env vars:
  EXIT_POLICY_STACK_ENABLED       0|1 (default 0 — safe, preserves existing behaviour)
  EXIT_PREMIUM_TARGET_PCT         float (default 0.015 = 1.5%)
  EXIT_TRAILING_ACTIVATION_PCT    float (default 0.01  = 1%)
  EXIT_TRAILING_TRAIL_PCT         float (default 0.005 = 0.5%)
  EXIT_THESIS_FAIL_BARS           int   (default 3)
  EXIT_THESIS_FAIL_MIN_MFE        float (default 0.002 = 0.2%)
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

from ..contracts import ExitReason, PositionContext
from ..market.snapshot_accessor import SnapshotAccessor

logger = logging.getLogger(__name__)


class ExitPolicy(ABC):
    @abstractmethod
    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        """Return ExitReason if position should be closed, else None."""
        ...

    @property
    @abstractmethod
    def name(self) -> str: ...


class PremiumTargetPolicy(ExitPolicy):
    """Exit when premium P&L reaches target_pct from entry. Locks in profits.

    Addresses the 17.5% MFE capture ratio observed 2026-06-01 (Trade 1: 4.15% MFE,
    only 0.52% captured). Fires well before the existing target_pct=0.80 threshold.
    """

    def __init__(self, target_pct: float = 0.015):
        self._target = target_pct

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.pnl_pct >= self._target:
            return ExitReason.TARGET_HIT
        return None

    @property
    def name(self) -> str:
        return f"premium_target_{self._target:.1%}"


class TrailingStopPolicy(ExitPolicy):
    """Once MFE exceeds activation_mfe, trail by trail_pct from peak.

    Protects captured profits while allowing winners to run.

    Example with activation=1%, trail=0.5%:
      MFE hits +1% → lock at +0.5%
      MFE hits +2% → lock at +1.5%
      Stop never moves backwards.

    Addresses trades 2, 6, 7 on 2026-06-01: MFE 1.14%→-0.64%, 1.06%→-0.49%, 1.61%→-0.17%.
    """

    def __init__(self, activation_mfe: float = 0.01, trail_pct: float = 0.005):
        self._activation = activation_mfe
        self._trail = trail_pct

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.mfe_pct < self._activation:
            return None
        if position.pnl_pct < position.mfe_pct - self._trail:
            return ExitReason.TRAILING_STOP
        return None

    @property
    def name(self) -> str:
        return f"trail_act={self._activation:.1%}_trail={self._trail:.1%}"


class ThesisFailPolicy(ExitPolicy):
    """Exit if trade shows no positive movement (MFE < min_mfe) after min_bars.

    Catches wrong-direction entries early: CE trades 3,4,5 on 2026-06-01 all had
    MFE=0% and would have been cut at bar 3 instead of running to full loss.

    Note: independent of the existing thesis_fail_exit_bars config path in tracker
    (which also requires pnl <= -8%). This policy uses a tighter pure-MFE check.
    """

    def __init__(self, min_bars: int = 3, min_mfe_pct: float = 0.002):
        self._min_bars = min_bars
        self._min_mfe = min_mfe_pct

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.bars_held >= self._min_bars and position.mfe_pct < self._min_mfe:
            return ExitReason.THESIS_FAIL
        return None

    @property
    def name(self) -> str:
        return f"thesis_fail_{self._min_bars}b_mfe={self._min_mfe:.1%}"


class CompositeExitPolicy(ExitPolicy):
    """Run all policies in order; first to trigger wins."""

    def __init__(self, policies: list[ExitPolicy]):
        self._policies = policies

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        for policy in self._policies:
            reason = policy.check(position, snap)
            if reason is not None:
                logger.debug("exit policy triggered: %s pos=%s pnl=%.3f mfe=%.3f bars=%d",
                             policy.name, position.position_id, position.pnl_pct,
                             position.mfe_pct, position.bars_held)
                return reason
        return None

    @property
    def name(self) -> str:
        return "composite[" + ",".join(p.name for p in self._policies) + "]"


# ─────────────────────────────────────────────────────────────────────────────
# LOTTERY MODE — asymmetric payoff: lose small often, win big rarely.
# Selected via EXIT_STRATEGY_MODE=lottery. The opposite philosophy of the
# scalper stack above: it does NOT cut winners early. Tight trailing/target are
# removed; winners are allowed to run to a big target or a loose giveback trail.
# ─────────────────────────────────────────────────────────────────────────────


class HardStopPolicy(ExitPolicy):
    """Cap the loss — the 'ticket price' you accept losing. Exit at -stop_pct.

    Set stop_pct >= 1.0 to disable (ride a lottery ticket all the way to zero).
    """

    def __init__(self, stop_pct: float = 0.25):
        self._stop = stop_pct

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if self._stop < 1.0 and position.pnl_pct <= -self._stop:
            return ExitReason.STOP_LOSS
        return None

    @property
    def name(self) -> str:
        return f"hard_stop_{self._stop:.0%}" if self._stop < 1.0 else "hard_stop_off"


class BigTargetPolicy(ExitPolicy):
    """Take the lottery win — exit when premium gains target_pct (e.g. +40%)."""

    def __init__(self, target_pct: float = 0.40):
        self._target = target_pct

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.pnl_pct >= self._target:
            return ExitReason.TARGET_HIT
        return None

    @property
    def name(self) -> str:
        return f"big_target_{self._target:.0%}"


class RunnerTrailPolicy(ExitPolicy):
    """Loose giveback trail — ONLY after a big move. Protects fat winners without
    choking them.

    Activates once MFE >= activation_mfe (e.g. +20%). Then locks a floor at
    mfe * (1 - giveback_frac): with giveback 0.40 and MFE 30%, floor = 18% — the
    trade can swing 20%->50% freely but won't round-trip a +30% winner back to 0.
    Far looser than the scalper's 0.5% trail.
    """

    def __init__(self, activation_mfe: float = 0.20, giveback_frac: float = 0.40):
        self._activation = activation_mfe
        self._giveback = giveback_frac

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.mfe_pct < self._activation:
            return None
        floor = position.mfe_pct * (1.0 - self._giveback)
        if position.pnl_pct < floor:
            return ExitReason.TRAILING_STOP
        return None

    @property
    def name(self) -> str:
        return f"runner_trail_act={self._activation:.0%}_give={self._giveback:.0%}"


class MomentumReversalPolicy(ExitPolicy):
    """Exit if the directional thesis broke — shadow score flipped hard against us.

    For a PE (bearish) bet, a strongly positive shadow score means momentum turned
    bullish: the lottery thesis is dead, cut it. Uses current_shadow_score which the
    tracker refreshes each bar. flip_threshold is the magnitude required.
    """

    def __init__(self, flip_threshold: float = 1.0):
        self._flip = flip_threshold

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        try:
            score = float(position.current_shadow_score)
        except (TypeError, ValueError):
            return None
        d = str(position.direction or "").upper()
        if d == "PE" and score >= self._flip:
            return ExitReason.REGIME_SHIFT
        if d == "CE" and score <= -self._flip:
            return ExitReason.REGIME_SHIFT
        return None

    @property
    def name(self) -> str:
        return f"momentum_flip_{self._flip:g}"


class TimestopPolicy(ExitPolicy):
    """Fallback — exit after max_bars. Lottery uses a longer hold than scalper so
    the rare big move has room to develop."""

    def __init__(self, max_bars: int = 25):
        self._max_bars = max_bars

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.bars_held >= self._max_bars:
            return ExitReason.TIME_STOP
        return None

    @property
    def name(self) -> str:
        return f"timestop_{self._max_bars}b"


def build_scalper_exit_stack() -> CompositeExitPolicy:
    """Scalper: capture small consistent gains, don't give them back.

    Order (first to trigger wins):
      1. ThesisFail   — cut dead entries early
      2. TrailingStop — ride winners; trails peak by trail_pct once activated
      3. PremiumTarget — emergency floor only (default 4%)
    """
    target_pct = float(os.getenv("EXIT_PREMIUM_TARGET_PCT", "0.04") or "0.04")
    activation_pct = float(os.getenv("EXIT_TRAILING_ACTIVATION_PCT", "0.01") or "0.01")
    trail_pct = float(os.getenv("EXIT_TRAILING_TRAIL_PCT", "0.005") or "0.005")
    thesis_bars = int(os.getenv("EXIT_THESIS_FAIL_BARS", "3") or "3")
    thesis_min_mfe = float(os.getenv("EXIT_THESIS_FAIL_MIN_MFE", "0.002") or "0.002")

    return CompositeExitPolicy([
        ThesisFailPolicy(thesis_bars, thesis_min_mfe),
        TrailingStopPolicy(activation_pct, trail_pct),
        PremiumTargetPolicy(target_pct),
    ])


def build_lottery_exit_stack() -> CompositeExitPolicy:
    """Lottery: lose small often, win big rarely. Let winners RUN.

    Order (first to trigger wins):
      1. HardStop        — cap the ticket loss
      2. ThesisFail      — cut dead/flat tickets (theta drain) but only when MFE
                           never showed promise (lottery min_mfe is higher)
      3. MomentumReversal — directional thesis broke
      4. BigTarget       — take the lottery win
      5. RunnerTrail     — loose giveback only after a big move
      6. Timestop        — EOD fallback (longer hold)
    """
    hard_stop = float(os.getenv("LOTTERY_HARD_STOP_PCT", "0.20") or "0.20")
    big_target = float(os.getenv("LOTTERY_BIG_TARGET_PCT", "0.50") or "0.50")
    runner_act = float(os.getenv("LOTTERY_RUNNER_ACTIVATION_MFE", "0.20") or "0.20")
    runner_give = float(os.getenv("LOTTERY_RUNNER_GIVEBACK_FRAC", "0.35") or "0.35")
    thesis_bars = int(os.getenv("LOTTERY_THESIS_FAIL_BARS", "5") or "5")
    thesis_min_mfe = float(os.getenv("LOTTERY_THESIS_FAIL_MIN_MFE", "0.03") or "0.03")
    flip = float(os.getenv("LOTTERY_MOMENTUM_FLIP", "1.0") or "1.0")
    timestop = int(os.getenv("LOTTERY_TIMESTOP_BARS", "90") or "90")

    policies: list[ExitPolicy] = [
        HardStopPolicy(hard_stop),
        ThesisFailPolicy(thesis_bars, thesis_min_mfe),
    ]
    if flip > 0:
        policies.append(MomentumReversalPolicy(flip))
    policies += [
        BigTargetPolicy(big_target),
        RunnerTrailPolicy(runner_act, runner_give),
        TimestopPolicy(timestop),
    ]
    return CompositeExitPolicy(policies)


class RegimeAdaptiveExitPolicy(ExitPolicy):
    """Route to lottery or scalper exit stack based on the regime at entry time.

    BREAKOUT / TRENDING → lottery: asymmetric payoff, let the move develop.
    Everything else      → scalper: capture small gains, cut losses fast.

    Controlled by EXIT_STRATEGY_MODE=adaptive. The set of lottery regimes can be
    overridden via ADAPTIVE_LOTTERY_REGIMES (comma-separated, default BREAKOUT,TRENDING).
    """

    _DEFAULT_LOTTERY_REGIMES = {"BREAKOUT", "TRENDING"}

    def __init__(self, scalper: CompositeExitPolicy, lottery: CompositeExitPolicy):
        self._scalper = scalper
        self._lottery = lottery
        raw = os.getenv("ADAPTIVE_LOTTERY_REGIMES", "") or ""
        if raw.strip():
            self._lottery_regimes = {r.strip().upper() for r in raw.split(",") if r.strip()}
        else:
            self._lottery_regimes = self._DEFAULT_LOTTERY_REGIMES

    def _stack_for(self, position: PositionContext) -> CompositeExitPolicy:
        regime = str(position.entry_regime or "").strip().upper()
        return self._lottery if regime in self._lottery_regimes else self._scalper

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        return self._stack_for(position).check(position, snap)

    @property
    def name(self) -> str:
        regimes = ",".join(sorted(self._lottery_regimes))
        return f"adaptive[lottery={regimes}|scalper=rest]"


def build_adaptive_exit_stack() -> RegimeAdaptiveExitPolicy:
    scalper = build_scalper_exit_stack()
    lottery = build_lottery_exit_stack()
    stack = RegimeAdaptiveExitPolicy(scalper, lottery)
    logger.info("exit policy mode=adaptive stack: %s", stack.name)
    return stack


def build_default_exit_stack() -> CompositeExitPolicy:
    """Build the exit stack for the configured EXIT_STRATEGY_MODE.

    EXIT_STRATEGY_MODE=scalper   (default) — capture small gains, don't give back.
    EXIT_STRATEGY_MODE=lottery             — lose small often, win big rarely.
    EXIT_STRATEGY_MODE=adaptive            — lottery on BREAKOUT/TRENDING, scalper otherwise.
    """
    mode = str(os.getenv("EXIT_STRATEGY_MODE", "scalper") or "scalper").strip().lower()
    if mode == "lottery":
        stack = build_lottery_exit_stack()
    elif mode == "adaptive":
        stack = build_adaptive_exit_stack()
    else:
        stack = build_scalper_exit_stack()
    logger.info("exit policy mode=%s stack: %s", mode, stack.name)
    return stack
