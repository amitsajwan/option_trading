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
from ..utils.env import as_bool

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


class StaleThesisTimestopPolicy(ExitPolicy):
    """Model-horizon timestop: the entry model predicts a >=threshold move within
    ~LABEL_HORIZON_MIN (15) minutes. If ``max_bars`` have elapsed and the position
    has not reached ``min_pnl``, the prediction has EXPIRED — holding further is
    unpriced risk the model was never validated on (2026-06-24 NIFTY: 21-min bleed
    to the -18% hard stop; 2026-07-01 BN: 4.7h hold to -14%). Winners are exempt:
    trailing/giveback manage them."""

    def __init__(self, max_bars: int = 15, min_pnl: float = 0.01):
        self._max_bars = max_bars
        self._min_pnl = min_pnl

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.bars_held >= self._max_bars and (position.pnl_pct or 0.0) < self._min_pnl:
            return ExitReason.TIME_STOP
        return None

    @property
    def name(self) -> str:
        return f"stale_thesis_{self._max_bars}b_pnl<{self._min_pnl:.1%}"


class GivebackStopPolicy(ExitPolicy):
    """Exit when a trade that showed initial promise grinds back past a giveback floor.

    Activates only after MFE >= min_mfe (default 3%) — the trade DID move in our
    favour. Once active, exits if pnl_pct falls below (mfe_pct - giveback_pct).

    Example with min_mfe=0.03, giveback_pct=0.09:
      MFE hits +3%  → active; floor = 3% - 9% = -6%. Exit if pnl < -6%.
      MFE hits +6%  → floor = 6% - 9% = -3%. Exit if pnl < -3%.
      MFE never 3%  → silent (thesis_fail handles the flat losers instead).

    This is the fix for the Jun 4 dead-zone: trade showed an initial +4% MFE then
    ground all the way to -18% before the hard floor fired. The giveback stop would
    have exited at -6% (MFE=4%, giveback=10% → floor=-6%).

    Distinct from TrailingStopPolicy (trail=0.5% from peak, scalper-tight) — this
    uses a much wider giveback tolerance and a higher activation threshold, so it only
    fires on the "initial promise then grind" pattern without choking scalper micro-moves.
    """

    def __init__(self, min_mfe: float = 0.03, giveback_pct: float = 0.09,
                 tiers: Optional[list[tuple[float, float]]] = None):
        self._min_mfe = min_mfe
        self._giveback = giveback_pct
        # Optional (mfe_threshold, giveback_pct) tiers, ascending by threshold.
        # 2026-07-16: a flat giveback tuned for "let a real winner run" (e.g. 15%)
        # gives almost no protection to a trade that only ever peaked at 5-8% MFE
        # -- floor sits below zero at the peak, so TIME_STOP/REGIME_SHIFT closes
        # it at whatever price they hit, not near the peak. Tiers keep the floor
        # tight while MFE is modest and only widen once it's proven itself.
        self._tiers = tiers

    def _giveback_for(self, mfe_pct: float) -> float:
        if not self._tiers:
            return self._giveback
        result = self._giveback
        for threshold, gb in self._tiers:
            if mfe_pct >= threshold:
                result = gb
        return result

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if position.mfe_pct < self._min_mfe:
            return None
        floor = position.mfe_pct - self._giveback_for(position.mfe_pct)
        if position.pnl_pct < floor:
            return ExitReason.TRAILING_STOP
        return None

    @property
    def name(self) -> str:
        if self._tiers:
            tiers_str = ",".join(f"{t:.0%}->{g:.0%}" for t, g in self._tiers)
            return f"giveback_act={self._min_mfe:.0%}_tiered[{tiers_str}]"
        return f"giveback_act={self._min_mfe:.0%}_give={self._giveback:.0%}"


class CompositeExitPolicy(ExitPolicy):
    """Run all policies in order; first to trigger wins.

    ``last_triggered`` carries the name of the specific policy that fired the
    most recent exit — several policies share an ExitReason enum value (e.g.
    MomentumReversalPolicy emits REGIME_SHIFT), so the enum alone cannot
    attribute an exit (2026-07-19: this ambiguity cost a full day of root-cause
    work). The tracker persists it as exit_policy_triggered.
    """

    def __init__(self, policies: list[ExitPolicy]):
        self._policies = policies
        self.last_triggered: Optional[str] = None

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        for policy in self._policies:
            reason = policy.check(position, snap)
            if reason is not None:
                self.last_triggered = policy.name
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

    min_bars: don't flip-exit before the entry model's prediction window has had a
    chance to play out (2026-07-17 finding: bar-1 REGIME_SHIFT exits killed entries
    whose direction was right for the next 10 minutes). HardStop still caps a
    genuinely broken trade during the protected bars.

    mode="reversal" (2026-07-19 root cause): the absolute check is NOT a reversal
    detector — an entry born disagreeing with the shadow score (contrarian model
    call, e.g. PE bought into an up-day pullback) trips it at the very first
    manage bar, on the same information the entry already accepted. All 17
    REGIME_SHIFT exits in the June+July pool came from this path (100% on
    BREAKOUT/TRENDING entries), 13 at bars=1, killing entries that were 60%
    right at the model's own 10-min horizon. Reversal mode fires only when the
    score is against the position AND has deteriorated >= reversal_delta since
    entry — genuine post-entry flips still exit immediately; born-disagreeing
    entries are sorted by ThesisFail (no-MFE cut) and HardStop instead.
    """

    def __init__(
        self,
        flip_threshold: float = 1.0,
        min_bars: int = 0,
        mode: str = "absolute",
        reversal_delta: float = 2.0,
    ):
        self._flip = flip_threshold
        self._min_bars = max(0, int(min_bars))
        self._mode = mode if mode in ("absolute", "reversal") else "absolute"
        self._delta = float(reversal_delta)

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        if int(position.bars_held or 0) < self._min_bars:
            return None
        try:
            score = float(position.current_shadow_score)
        except (TypeError, ValueError):
            return None
        d = str(position.direction or "").upper()
        against = (d == "PE" and score >= self._flip) or (d == "CE" and score <= -self._flip)
        if not against:
            return None
        if self._mode == "reversal":
            entry_score = position.entry_shadow_score
            if entry_score is not None:
                moved_against = (score - float(entry_score)) if d == "PE" else (float(entry_score) - score)
                if moved_against < self._delta:
                    return None
        return ExitReason.REGIME_SHIFT

    @property
    def name(self) -> str:
        return (
            f"momentum_flip_{self._flip:g}"
            + (f"_minbars{self._min_bars}" if self._min_bars else "")
            + (f"_rev{self._delta:g}" if self._mode == "reversal" else "")
        )


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
      1. HardStop     — cap the loss so the stack is self-sufficient (no reliance
                        on the tracker's legacy inline stop-losses). Disabled when
                        EXIT_SCALPER_HARD_STOP_PCT >= 1.0.
      2. ThesisFail   — cut dead entries early
      3. GivebackStop — exit trades that showed initial promise but then ground back
                        (the Jun-4 dead-zone fix). Gated by EXIT_GIVEBACK_STOP_ENABLED.
      4. TrailingStop — ride winners; trails peak by trail_pct once activated
      5. PremiumTarget — emergency floor only (default 4%)
    """
    hard_stop = float(os.getenv("EXIT_SCALPER_HARD_STOP_PCT", "0.25") or "0.25")
    target_pct = float(os.getenv("EXIT_PREMIUM_TARGET_PCT", "0.04") or "0.04")
    activation_pct = float(os.getenv("EXIT_TRAILING_ACTIVATION_PCT", "0.01") or "0.01")
    trail_pct = float(os.getenv("EXIT_TRAILING_TRAIL_PCT", "0.005") or "0.005")
    thesis_bars = int(os.getenv("EXIT_THESIS_FAIL_BARS", "3") or "3")
    thesis_min_mfe = float(os.getenv("EXIT_THESIS_FAIL_MIN_MFE", "0.002") or "0.002")
    giveback_enabled = as_bool(os.getenv("EXIT_GIVEBACK_STOP_ENABLED", "false"))
    giveback_min_mfe = float(os.getenv("EXIT_GIVEBACK_MIN_MFE", "0.03") or "0.03")
    giveback_pct = float(os.getenv("EXIT_GIVEBACK_PCT", "0.09") or "0.09")
    # Model-horizon timestop (0 = off): exit losing/flat positions once the entry
    # model's prediction window (15 min) has expired. Winners are exempt.
    stale_bars = int(os.getenv("EXIT_SCALPER_STALE_BARS", "0") or "0")
    stale_min_pnl = float(os.getenv("EXIT_SCALPER_STALE_MIN_PNL", "0.01") or "0.01")

    policies: list[ExitPolicy] = [
        HardStopPolicy(hard_stop),
        ThesisFailPolicy(thesis_bars, thesis_min_mfe),
    ]
    if giveback_enabled:
        policies.append(GivebackStopPolicy(giveback_min_mfe, giveback_pct))
    if stale_bars > 0:
        policies.append(StaleThesisTimestopPolicy(stale_bars, stale_min_pnl))
    policies += [
        TrailingStopPolicy(activation_pct, trail_pct),
        PremiumTargetPolicy(target_pct),
    ]
    return CompositeExitPolicy(policies)


def build_lottery_exit_stack() -> CompositeExitPolicy:
    """Lottery: lose small often, win big rarely. Let winners RUN.

    Order (first to trigger wins):
      1. HardStop        — cap the ticket loss
      2. ThesisFail      — cut dead/flat tickets (theta drain) but only when MFE
                           never showed promise (lottery min_mfe is higher)
      3. GivebackStop    — exit trades that showed initial promise but then ground back.
                           Uses wider giveback than scalper (default 15%) so it doesn't
                           choke a genuine lottery winner. Gated by EXIT_GIVEBACK_STOP_ENABLED.
      4. MomentumReversal — directional thesis broke
      5. BigTarget       — take the lottery win
      6. RunnerTrail     — loose giveback only after a big move
      7. Timestop        — EOD fallback (longer hold)
    """
    hard_stop = float(os.getenv("LOTTERY_HARD_STOP_PCT", "0.20") or "0.20")
    big_target = float(os.getenv("LOTTERY_BIG_TARGET_PCT", "0.50") or "0.50")
    runner_act = float(os.getenv("LOTTERY_RUNNER_ACTIVATION_MFE", "0.20") or "0.20")
    runner_give = float(os.getenv("LOTTERY_RUNNER_GIVEBACK_FRAC", "0.35") or "0.35")
    thesis_bars = int(os.getenv("LOTTERY_THESIS_FAIL_BARS", "5") or "5")
    thesis_min_mfe = float(os.getenv("LOTTERY_THESIS_FAIL_MIN_MFE", "0.03") or "0.03")
    flip = float(os.getenv("LOTTERY_MOMENTUM_FLIP", "1.0") or "1.0")
    flip_min_bars = int(os.getenv("LOTTERY_MOMENTUM_FLIP_MIN_BARS", "0") or "0")
    flip_mode = str(os.getenv("LOTTERY_MOMENTUM_FLIP_MODE", "absolute") or "absolute").strip().lower()
    flip_delta = float(os.getenv("LOTTERY_MOMENTUM_FLIP_DELTA", "2.0") or "2.0")
    timestop = int(os.getenv("LOTTERY_TIMESTOP_BARS", "90") or "90")
    giveback_enabled = as_bool(os.getenv("EXIT_GIVEBACK_STOP_ENABLED", "false"))
    giveback_min_mfe = float(os.getenv("EXIT_GIVEBACK_MIN_MFE", "0.03") or "0.03")
    giveback_pct = float(os.getenv("LOTTERY_GIVEBACK_PCT", "0.15") or "0.15")
    # 2026-07-16: flat giveback (tuned for the rare big winner) gives almost no
    # protection to trades that only ever peak modest -- floor sits below zero
    # at a 5-8% peak, so TIME_STOP/REGIME_SHIFT closes them well off the peak
    # instead of the giveback stop locking something in. Opt-in tiering keeps
    # the floor tight for modest MFE and only widens once it's proven itself,
    # without touching the big-winner case (still 15% once MFE clears 20%).
    giveback_tiered = as_bool(os.getenv("LOTTERY_GIVEBACK_TIERED", "false"))
    giveback_tiers = None
    if giveback_tiered:
        t1 = float(os.getenv("LOTTERY_GIVEBACK_TIER1_PCT", "0.02") or "0.02")
        t2 = float(os.getenv("LOTTERY_GIVEBACK_TIER2_PCT", "0.05") or "0.05")
        t2_mfe = float(os.getenv("LOTTERY_GIVEBACK_TIER2_MFE", "0.10") or "0.10")
        t3_mfe = float(os.getenv("LOTTERY_GIVEBACK_TIER3_MFE", "0.20") or "0.20")
        giveback_tiers = [(giveback_min_mfe, t1), (t2_mfe, t2), (t3_mfe, giveback_pct)]

    policies: list[ExitPolicy] = [
        HardStopPolicy(hard_stop),
        ThesisFailPolicy(thesis_bars, thesis_min_mfe),
    ]
    if giveback_enabled:
        policies.append(GivebackStopPolicy(giveback_min_mfe, giveback_pct, tiers=giveback_tiers))
    if flip > 0:
        policies.append(MomentumReversalPolicy(flip, min_bars=flip_min_bars, mode=flip_mode, reversal_delta=flip_delta))
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

    CRITICAL: BREAKOUT and TRENDING entries use the LOTTERY hard stop (LOTTERY_HARD_STOP_PCT,
    default 20%), NOT the scalper hard stop (EXIT_SCALPER_HARD_STOP_PCT). Setting only
    EXIT_SCALPER_HARD_STOP_PCT=0.05 gives ZERO protection on BREAKOUT/TRENDING trades —
    they bypass the scalper stack entirely and run the 20% lottery stop.
    Root cause of the 2026-06-05 incident where a BREAKOUT entry ran to -13.2% unstopped
    while EXIT_SCALPER_HARD_STOP_PCT=0.05 was set. Fix: also set LOTTERY_HARD_STOP_PCT
    to your desired max loss, or exclude BREAKOUT from ADAPTIVE_LOTTERY_REGIMES.
    """

    _DEFAULT_LOTTERY_REGIMES = {"BREAKOUT", "TRENDING"}

    def __init__(self, scalper: CompositeExitPolicy, lottery: CompositeExitPolicy):
        self._scalper = scalper
        self._lottery = lottery
        self.last_triggered: Optional[str] = None
        raw = os.getenv("ADAPTIVE_LOTTERY_REGIMES", "") or ""
        if raw.strip():
            self._lottery_regimes = {r.strip().upper() for r in raw.split(",") if r.strip()}
        else:
            self._lottery_regimes = self._DEFAULT_LOTTERY_REGIMES

    def _stack_for(self, position: PositionContext) -> CompositeExitPolicy:
        regime = str(position.entry_regime or "").strip().upper()
        return self._lottery if regime in self._lottery_regimes else self._scalper

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        stack = self._stack_for(position)
        reason = stack.check(position, snap)
        # Propagate the firing policy's name so the tracker can persist it.
        self.last_triggered = stack.last_triggered if reason is not None else None
        return reason

    @property
    def name(self) -> str:
        regimes = ",".join(sorted(self._lottery_regimes))
        return f"adaptive[lottery={regimes}|scalper=rest]"


class ExpiryAwareExitPolicy(ExitPolicy):
    """Route to a tighter 'expiry' stack when near expiry, else the normal stack.

    Near expiry, theta dominates and ATM premium decays non-linearly: a stalled
    thesis bleeds far faster than on a fresh DTE. So when the snapshot is within
    ``dte_threshold`` days of expiry we swap in a tighter stack (faster thesis
    fail, tighter hard stop, earlier trail). Gated by EXIT_EXPIRY_OVERRIDE_ENABLED.

    dte_threshold semantics:
      0  → expiry day only (uses snap.is_expiry_day, robust when DTE is missing)
      N  → snap.days_to_expiry <= N
    When DTE is unavailable and threshold > 0, falls back to the normal stack.
    """

    def __init__(self, normal: ExitPolicy, expiry: ExitPolicy, dte_threshold: int = 0):
        self._normal = normal
        self._expiry = expiry
        self._dte_threshold = dte_threshold

    def _is_expiry(self, snap: SnapshotAccessor) -> bool:
        try:
            if self._dte_threshold <= 0:
                return bool(snap.is_expiry_day)
            dte = snap.days_to_expiry
            return dte is not None and int(dte) <= self._dte_threshold
        except Exception:
            return False

    def check(self, position: PositionContext, snap: SnapshotAccessor) -> Optional[ExitReason]:
        stack = self._expiry if self._is_expiry(snap) else self._normal
        return stack.check(position, snap)

    @property
    def name(self) -> str:
        return f"expiry_aware[dte<={self._dte_threshold}->{self._expiry.name}|else={self._normal.name}]"


def build_expiry_exit_stack() -> CompositeExitPolicy:
    """Tight stack for near-expiry positions — cut stalls fast, trail early.

    Theta is brutal at low DTE, so this is deliberately tighter than scalper:
    smaller hard stop, fewer thesis-fail bars, earlier/tighter trail.
    """
    hard_stop = float(os.getenv("EXIT_EXPIRY_HARD_STOP_PCT", "0.15") or "0.15")
    thesis_bars = int(os.getenv("EXIT_EXPIRY_THESIS_FAIL_BARS", "3") or "3")
    thesis_min_mfe = float(os.getenv("EXIT_EXPIRY_THESIS_FAIL_MIN_MFE", "0.02") or "0.02")
    activation_pct = float(os.getenv("EXIT_EXPIRY_TRAIL_ACTIVATION_PCT", "0.03") or "0.03")
    trail_pct = float(os.getenv("EXIT_EXPIRY_TRAIL_PCT", "0.015") or "0.015")
    target_pct = float(os.getenv("EXIT_EXPIRY_PREMIUM_TARGET_PCT", "0.10") or "0.10")

    return CompositeExitPolicy([
        HardStopPolicy(hard_stop),
        ThesisFailPolicy(thesis_bars, thesis_min_mfe),
        TrailingStopPolicy(activation_pct, trail_pct),
        PremiumTargetPolicy(target_pct),
    ])


def build_adaptive_exit_stack() -> RegimeAdaptiveExitPolicy:
    scalper = build_scalper_exit_stack()
    lottery = build_lottery_exit_stack()
    stack = RegimeAdaptiveExitPolicy(scalper, lottery)
    logger.info("exit policy mode=adaptive stack: %s", stack.name)
    return stack


def build_default_exit_stack() -> ExitPolicy:
    """Build the exit stack for the configured EXIT_STRATEGY_MODE.

    EXIT_STRATEGY_MODE=scalper   (default) — capture small gains, don't give back.
    EXIT_STRATEGY_MODE=lottery             — lose small often, win big rarely.
    EXIT_STRATEGY_MODE=adaptive            — lottery on BREAKOUT/TRENDING, scalper otherwise.

    When EXIT_EXPIRY_OVERRIDE_ENABLED=1 the chosen stack is wrapped so that
    near-expiry snapshots (within EXIT_EXPIRY_DTE_THRESHOLD days) route to a
    tighter expiry stack — theta is brutal at low DTE.

    UNIVERSAL MAX-LOSS FLOOR (EXIT_MAX_LOSS_PCT, default 0.10):
    A mode/regime-independent hard stop is ALWAYS checked first, wrapping the whole
    stack. This closes the 2026-06-05 footgun where EXIT_SCALPER_HARD_STOP_PCT only
    protected the scalper stack, so adaptive-mode BREAKOUT/TRENDING trades fell
    through to the lottery 20% stop and one ran to -13%. No regime/mode path can now
    leave a trade unprotected. Set EXIT_MAX_LOSS_PCT>=1.0 to disable (not advised live).
    """
    mode = str(os.getenv("EXIT_STRATEGY_MODE", "scalper") or "scalper").strip().lower()
    if mode == "lottery":
        stack: ExitPolicy = build_lottery_exit_stack()
    elif mode == "adaptive":
        stack = build_adaptive_exit_stack()
    else:
        stack = build_scalper_exit_stack()

    if as_bool(os.getenv("EXIT_EXPIRY_OVERRIDE_ENABLED", "false")):
        dte_threshold = int(os.getenv("EXIT_EXPIRY_DTE_THRESHOLD", "0") or "0")
        stack = ExpiryAwareExitPolicy(
            normal=stack,
            expiry=build_expiry_exit_stack(),
            dte_threshold=dte_threshold,
        )

    max_loss = float(os.getenv("EXIT_MAX_LOSS_PCT", "0.10") or "0.10")
    if max_loss < 1.0:
        # Floor checked FIRST so it overrides any looser mode/regime stop.
        stack = CompositeExitPolicy([HardStopPolicy(max_loss), stack])

    logger.info(
        "exit policy mode=%s max_loss_floor=%s stack: %s",
        mode,
        f"{max_loss:.0%}" if max_loss < 1.0 else "off",
        stack.name,
    )
    return stack
