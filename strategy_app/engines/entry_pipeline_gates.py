"""Entry-pipeline gate implementations and evaluate_v2().

Gate classes wrap the *existing* engine helpers unchanged — they are a
structural refactor, not a behaviour change.  Each gate delegates to the same
underlying logic already used by the three old entry paths; the only new thing
is (a) a single candidate loop and (b) typed, structured trace emission.

Activation: set ``STRATEGY_ENTRY_PIPELINE_V2=1`` on the engine.
Default is ``0`` (old paths run) so this is completely non-breaking.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..contracts import Direction, RiskContext, SignalType, StrategyVote, TradeSignal
from ..market.regime import RegimeSignal
from ..market.snapshot_accessor import SnapshotAccessor
from .entry_pipeline_contracts import (
    EntryContext,
    Gate,
    GateOutcome,
    GateResult,
    run_chain,
)
from .entry_config import EntryConfig

if TYPE_CHECKING:
    from .direction_consensus import DirectionConsensusResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gate 1 — HardGatesGate
# ---------------------------------------------------------------------------

class HardGatesGate(Gate):
    """Env / session / phase hard gates.

    Any failure here is a VETO — the whole bar is dead.
    Checks: valid entry phase, risk not paused, time window, regime tag,
    regime confidence.
    """

    name = "HardGates"

    def apply(self, ctx: EntryContext) -> GateResult:
        snap = ctx.snap
        regime = ctx.regime
        cfg = ctx.config

        if not snap.is_valid_entry_phase:
            return GateResult.veto("invalid_entry_phase")

        if ctx.risk.daily_loss_breached:
            return GateResult.veto("daily_loss_breached")

        ts = snap.timestamp
        if ts is not None and cfg.entry_time_windows:
            mins = ts.hour * 60 + ts.minute
            if not cfg.time_window_allows(mins):
                return GateResult.veto(
                    "outside_time_window",
                    time_mins=mins,
                    windows=cfg.entry_time_windows,
                )

        return GateResult.ok()


# ---------------------------------------------------------------------------
# Gate 2 — VotesGate
# ---------------------------------------------------------------------------

class VotesGate(Gate):
    """Require at least one usable ENTRY vote (CE or PE direction).

    VETO if the vote pool is empty — nothing to act on.
    """

    name = "Votes"

    def apply(self, ctx: EntryContext) -> GateResult:
        entry_votes = [
            v for v in ctx.votes
            if v.signal_type == SignalType.ENTRY
            and v.direction in (Direction.CE, Direction.PE)
        ]
        if not entry_votes:
            return GateResult.veto("no_entry_votes", total_votes=len(ctx.votes))
        return GateResult.ok()


# ---------------------------------------------------------------------------
# Gate 3 — RegimeConfidenceGate
# ---------------------------------------------------------------------------

class RegimeConfidenceGate(Gate):
    """Block on low regime confidence (unless profile relaxes it).

    This is separated from HardGatesGate so profiles can inject/skip it.
    """

    name = "RegimeConf"

    def __init__(self, relax: bool = False) -> None:
        self._relax = relax

    def apply(self, ctx: EntryContext) -> GateResult:
        if self._relax:
            return GateResult.ok()
        regime = ctx.regime
        cfg = ctx.config
        if regime.confidence < cfg.regime_min_confidence:
            return GateResult.veto(
                "low_regime_confidence",
                confidence=round(regime.confidence, 3),
                required=cfg.regime_min_confidence,
                regime=getattr(regime.regime, "value", str(regime.regime)),
            )
        return GateResult.ok()


# ---------------------------------------------------------------------------
# Gate 4a — MLEntryGate (ML-FIRST: run the ML/vol entry trigger before direction)
# ---------------------------------------------------------------------------

class MLEntryGate(Gate):
    """ML-first entry trigger: require a usable ML_ENTRY / VOL_GATE_ENTRY vote that
    clears the bypass confidence threshold, BEFORE any direction work is done.

    Splitting this out of DirectionGate gives a clean, ordered trace — you see
    exactly whether a bar died on the ML threshold (``no_ml_entry_vote`` /
    ``ml_confidence_below_bypass``) or later on direction. Stashes the winning
    vote on ``ctx.ml_vote`` for DirectionGate to consume.

    For non-consensus profiles the ML trigger is not required (the candidate's own
    direction is used downstream), so this gate PASSes through untouched.
    """

    name = "MLEntry"

    def __init__(self, is_consensus: bool = True) -> None:
        self._is_consensus = is_consensus

    def apply(self, ctx: EntryContext) -> GateResult:
        if not self._is_consensus:
            return GateResult.ok()

        cfg = ctx.config
        # ML_ENTRY and VOL_GATE_ENTRY are interchangeable entry triggers (same
        # bypass pipeline); whichever is active produces the entry vote.
        ml_votes = [v for v in ctx.votes if v.strategy_name in ("ML_ENTRY", "VOL_GATE_ENTRY")]
        if not ml_votes:
            return GateResult.veto("no_ml_entry_vote")

        ml_vote = max(ml_votes, key=lambda v: float(v.confidence or 0))

        if ml_vote.confidence < cfg.bypass_min_confidence:
            return GateResult.veto(
                "ml_confidence_below_bypass",
                ml_confidence=round(float(ml_vote.confidence), 3),
                bypass_min=cfg.bypass_min_confidence,
            )

        ctx.ml_vote = ml_vote
        return GateResult.ok()


# ---------------------------------------------------------------------------
# Gate 4b — DirectionGate (resolve CE/PE AFTER the ML trigger passed)
# ---------------------------------------------------------------------------

class DirectionGate(Gate):
    """Resolve CE/PE direction via consensus (ML advisory + rules + shadow).

    Runs AFTER MLEntryGate, consuming the vote it stashed on ``ctx.ml_vote``.
    Writes ``ctx.direction`` on PASS. VETO on consensus.vetoed or no direction.

    Requires the engine to provide callbacks for shadow scoring and consensus:
    - ``shadow_fn(snap) -> (Direction, str, float)``
    - ``ml_hint_fn(vote) -> (Optional[Direction], Optional[float])``
    - ``consensus_fn(**kwargs) -> DirectionConsensusResult``
    """

    name = "Direction"

    def __init__(
        self,
        shadow_fn: Callable,
        ml_hint_fn: Callable,
        consensus_fn: Callable,
        ml_entry_vote_selector: Callable,
        is_consensus: bool = True,
    ) -> None:
        self._shadow_fn = shadow_fn
        self._ml_hint_fn = ml_hint_fn
        self._consensus_fn = consensus_fn
        self._ml_vote_selector = ml_entry_vote_selector
        self._is_consensus = is_consensus

    def apply(self, ctx: EntryContext) -> GateResult:
        snap = ctx.snap
        regime = ctx.regime
        cfg = ctx.config

        # Bug-1 fix: the consensus-bypass path (ML_ENTRY vote required + 0.80 bypass
        # threshold) is v1's behaviour ONLY for consensus profiles
        # (_PROFILES_ML_ENTRY_CONSENSUS). For every other profile, v1 takes the
        # scored/sequential path where direction is the candidate vote's OWN direction
        # — no ML_ENTRY requirement, no bypass gate. Mirror that here so v2 stops
        # over-vetoing real trades (see docs/ENTRY_PIPELINE_V1_V2_ANALYSIS.md bug 1).
        if not self._is_consensus:
            cand = ctx.candidate
            if cand is None or cand.direction not in (Direction.CE, Direction.PE):
                return GateResult.skip("candidate_no_direction")
            ctx.direction = cand.direction
            return GateResult.ok()

        # MLEntryGate (runs first) stashed the winning vote. Fall back to selecting
        # it here so DirectionGate still works if used without MLEntryGate.
        ml_vote = ctx.ml_vote
        if ml_vote is None:
            ml_votes = [v for v in ctx.votes if v.strategy_name in ("ML_ENTRY", "VOL_GATE_ENTRY")]
            if not ml_votes:
                return GateResult.veto("no_ml_entry_vote")
            ml_vote = max(ml_votes, key=lambda v: float(v.confidence or 0))
            if ml_vote.confidence < cfg.bypass_min_confidence:
                return GateResult.veto(
                    "ml_confidence_below_bypass",
                    ml_confidence=round(float(ml_vote.confidence), 3),
                    bypass_min=cfg.bypass_min_confidence,
                )

        shadow_dir, shadow_basis, shadow_score = self._shadow_fn(snap)
        hint_dir, ce_prob = self._ml_hint_fn(ml_vote)

        rule_votes = [
            v for v in ctx.votes
            if v.strategy_name != "ML_ENTRY"
            and v.direction in (Direction.CE, Direction.PE)
        ]

        consensus = self._consensus_fn(
            snap=snap,
            rule_votes=rule_votes,
            shadow_direction=shadow_dir,
            shadow_score=shadow_score,
            ml_direction_hint=hint_dir,
            ml_ce_prob=ce_prob,
            regime_signal=regime,
        )

        if consensus.vetoed or consensus.direction is None:
            return GateResult.veto(
                "direction_consensus_vetoed",
                veto_reason=consensus.veto_reason,
                ce_score=round(consensus.ce_score, 3),
                pe_score=round(consensus.pe_score, 3),
                margin=round(consensus.margin, 3),
                shadow_basis=shadow_basis,
            )

        ctx.direction = consensus.direction

        if isinstance(ml_vote.raw_signals, dict):
            ml_vote.raw_signals.update({
                "direction_source": "direction_consensus",
                "direction_consensus_ce": round(consensus.ce_score, 3),
                "direction_consensus_pe": round(consensus.pe_score, 3),
                "direction_consensus_margin": round(consensus.margin, 3),
                "direction_consensus_shadow_basis": shadow_basis,
                "direction_consensus_sources": {
                    k: round(v, 3) for k, v in (consensus.sources or {}).items()
                },
            })

        return GateResult.ok()


# ---------------------------------------------------------------------------
# Gate 5 — StrikeDepthGate
# ---------------------------------------------------------------------------

class StrikeDepthGate(Gate):
    """Select a strike; enforce premium hard cap; handle IV veto.

    Writes ``ctx.strike`` and ``ctx.premium`` on PASS.
    VETO on: IV reject, no affordable strike under hard cap, no strike found.

    This is the gate that fixes the silent ₹500-cap bug — the premium check
    is explicit here and always logged with `decision_id`.
    """

    name = "StrikeDepth"

    def __init__(self, apply_strike_fn: Callable) -> None:
        self._apply_strike_fn = apply_strike_fn

    def apply(self, ctx: EntryContext) -> GateResult:
        candidate = ctx.candidate
        snap = ctx.snap
        cfg = ctx.config

        if candidate is None:
            return GateResult.veto("no_candidate")

        if candidate.direction not in (Direction.CE, Direction.PE):
            return GateResult.veto("candidate_no_direction")

        candidate_dir = candidate.direction

        if ctx.direction is not None and candidate_dir != ctx.direction:
            candidate.direction = ctx.direction
            candidate_dir = ctx.direction

        self._apply_strike_fn(
            candidate, snap,
            regime=getattr(ctx.regime.regime, "value", str(ctx.regime.regime)),
        )

        if candidate.raw_signals.get("_strike_vetoed"):
            strike_reason = candidate.raw_signals.get("_strike_veto_reason", "unknown")
            atm_ltp = snap.option_ltp(candidate_dir.value, snap.atm_strike or 0) if snap.atm_strike else None
            return GateResult.veto(
                "strike_vetoed",
                strike_veto_reason=strike_reason,
                atm_strike=snap.atm_strike,
                atm_ltp=atm_ltp,
                max_premium=cfg.max_premium,
                decision_id=ctx.decision_id,
            )

        strike = candidate.proposed_strike or snap.atm_strike
        if strike is None or int(strike) <= 0:
            return GateResult.veto("no_strike", atm=snap.atm_strike)
        strike = int(strike)

        premium = candidate.proposed_entry_premium
        if premium is None or premium <= 0:
            premium = snap.option_ltp(candidate_dir.value, strike)
        if premium is None or premium <= 0:
            return GateResult.veto("no_premium", strike=strike, dir=candidate_dir.value)

        if cfg.hard_premium_cap and cfg.max_premium > 0 and premium > cfg.max_premium:
            return GateResult.veto(
                "premium_exceeds_hard_cap",
                premium=round(premium, 2),
                max_premium=cfg.max_premium,
                strike=strike,
                dir=candidate_dir.value,
                decision_id=ctx.decision_id,
            )

        ctx.strike = strike
        ctx.premium = float(premium)
        candidate.proposed_strike = strike
        candidate.proposed_entry_premium = ctx.premium

        return GateResult.ok()


# ---------------------------------------------------------------------------
# Gate 6 — EntryPolicyGate
# ---------------------------------------------------------------------------

class EntryPolicyGate(Gate):
    """Delegate to the existing EntryPolicy.

    Returns SKIP_CANDIDATE (not VETO) on failure — another vote may pass.
    """

    name = "EntryPolicy"

    def __init__(self, policy_fn: Callable) -> None:
        self._policy_fn = policy_fn

    def apply(self, ctx: EntryContext) -> GateResult:
        candidate = ctx.candidate
        if candidate is None:
            return GateResult.skip("no_candidate")

        decision = self._policy_fn(candidate, ctx.snap, ctx.regime, ctx.risk)

        candidate.raw_signals["_policy_allowed"] = decision.allowed
        candidate.raw_signals["_policy_score"] = round(decision.score, 3)
        candidate.raw_signals["_policy_reason"] = decision.reason
        candidate.raw_signals["_policy_checks"] = dict(decision.checks)

        if not decision.allowed:
            return GateResult.skip(
                "entry_policy_blocked",
                policy_reason=decision.reason,
                policy_score=round(decision.score, 3),
                strategy=candidate.strategy_name,
            )
        return GateResult.ok()


# ---------------------------------------------------------------------------
# Gate 7 — ConfidenceGate
# ---------------------------------------------------------------------------

class ConfidenceGate(Gate):
    """Block candidates below min_confidence.

    Returns SKIP_CANDIDATE — a higher-confidence vote may still pass.
    """

    name = "Confidence"

    def apply(self, ctx: EntryContext) -> GateResult:
        candidate = ctx.candidate
        if candidate is None:
            return GateResult.skip("no_candidate")

        cfg = ctx.config
        if float(candidate.confidence) < cfg.min_confidence:
            return GateResult.skip(
                "below_min_confidence",
                confidence=round(float(candidate.confidence), 3),
                min_confidence=cfg.min_confidence,
                strategy=candidate.strategy_name,
            )
        return GateResult.ok()


# ---------------------------------------------------------------------------
# evaluate_v2() — the single-loop pipeline runner
# ---------------------------------------------------------------------------

def _log_gate_outcome(
    ctx: EntryContext,
    result: GateResult,
    candidate_name: Optional[str],
) -> None:
    """Emit one structured line per non-pass gate outcome."""
    if result.outcome == GateOutcome.PASS:
        return
    vals_str = " ".join(f"{k}={v}" for k, v in result.values.items())
    logger.info(
        "entry_gate decision_id=%s stage=%s outcome=%s reason=%s candidate=%s %s",
        ctx.decision_id,
        ctx.trace[-1].gate_name if ctx.trace else "?",
        result.outcome.value,
        result.reason,
        candidate_name or "—",
        vals_str,
    )


def build_entry_pipeline(
    *,
    regime_min_relax: bool = False,
    is_consensus: bool = True,
    shadow_fn: Callable,
    ml_hint_fn: Callable,
    consensus_fn: Callable,
    ml_entry_vote_selector: Callable,
    apply_strike_fn: Callable,
    policy_fn: Callable,
) -> list[Gate]:
    """Construct the default ENTRY_PIPELINE gate list."""
    return [
        HardGatesGate(),
        VotesGate(),
        RegimeConfidenceGate(relax=regime_min_relax),
        # ML-FIRST: the ML/vol entry trigger runs as its own gate before direction,
        # so a bar that fails the ML threshold never reaches direction resolution.
        MLEntryGate(is_consensus=is_consensus),
        DirectionGate(
            shadow_fn=shadow_fn,
            ml_hint_fn=ml_hint_fn,
            consensus_fn=consensus_fn,
            ml_entry_vote_selector=ml_entry_vote_selector,
            is_consensus=is_consensus,
        ),
        StrikeDepthGate(apply_strike_fn=apply_strike_fn),
        EntryPolicyGate(policy_fn=policy_fn),
        ConfidenceGate(),
    ]


def evaluate_v2(
    *,
    ctx: EntryContext,
    gates: list[Gate],
    build_signal_fn: Callable,
    rank_votes_fn: Optional[Callable] = None,
) -> Optional[TradeSignal]:
    """Single-loop pipeline runner — replaces the three old entry paths.

    For each ranked candidate vote:
    - Run the full gate chain.
    - VETO  → bar is dead, return None immediately.
    - SKIP_CANDIDATE → advance to next vote.
    - PASS (all gates) → build and return the TradeSignal.

    ``build_signal_fn(ctx) -> Optional[TradeSignal]``
    ``rank_votes_fn(votes) -> list[StrategyVote]`` (default: sort by confidence desc)
    """
    entry_votes = [
        v for v in ctx.votes
        if v.signal_type == SignalType.ENTRY
        and v.direction in (Direction.CE, Direction.PE)
    ]

    if rank_votes_fn is not None:
        ranked = rank_votes_fn(entry_votes)
    else:
        ranked = sorted(entry_votes, key=lambda v: float(v.confidence or 0), reverse=True)

    for vote in ranked:
        ctx.reset_candidate(vote)

        result = run_chain(ctx, gates)
        _log_gate_outcome(ctx, result, candidate_name=vote.strategy_name)

        if result.outcome == GateOutcome.VETO:
            logger.debug(
                "entry_pipeline veto decision_id=%s reason=%s",
                ctx.decision_id,
                result.reason,
            )
            return None

        if result.outcome == GateOutcome.SKIP_CANDIDATE:
            continue

        signal = build_signal_fn(ctx)
        if signal is not None:
            logger.debug(
                "entry_pipeline pass decision_id=%s strategy=%s dir=%s strike=%s",
                ctx.decision_id,
                vote.strategy_name,
                ctx.direction,
                ctx.strike,
            )
        return signal

    return None
