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


def build_default_exit_stack() -> CompositeExitPolicy:
    """Build exit stack from env vars. Called once at engine startup.

    Order matters — first to trigger wins:
      1. ThesisFail   — cut dead entries early (MFE never moved after N bars)
      2. TrailingStop — ride winners; trails peak by trail_pct once activated
      3. PremiumTarget — emergency floor only; set high (default 4%) so it
                         never fires before TrailingStop can do its job

    PremiumTarget at 1.5% was capping runners: it fired before TrailingStop
    could trail up to 3-4%. Raised default to 0.04 (4%) — acts as safety net
    only on very fast moves where TrailingStop hasn't activated yet.
    """
    target_pct = float(os.getenv("EXIT_PREMIUM_TARGET_PCT", "0.04") or "0.04")
    activation_pct = float(os.getenv("EXIT_TRAILING_ACTIVATION_PCT", "0.01") or "0.01")
    trail_pct = float(os.getenv("EXIT_TRAILING_TRAIL_PCT", "0.005") or "0.005")
    thesis_bars = int(os.getenv("EXIT_THESIS_FAIL_BARS", "3") or "3")
    thesis_min_mfe = float(os.getenv("EXIT_THESIS_FAIL_MIN_MFE", "0.002") or "0.002")

    stack = CompositeExitPolicy([
        ThesisFailPolicy(thesis_bars, thesis_min_mfe),
        TrailingStopPolicy(activation_pct, trail_pct),
        PremiumTargetPolicy(target_pct),
    ])
    logger.info("exit policy stack: %s", stack.name)
    return stack
