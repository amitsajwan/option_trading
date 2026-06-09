"""Aggregate CE/PE direction hints; veto when side is unclear.

ML entry timing is separate (ML_ENTRY vote). Direction comes from rule strategies,
shadow/momentum hints, and optional direction-ML as a *weak* vote — not a dictator.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ..contracts import Direction, StrategyVote
from ..market.regime import Regime, RegimeSignal
from ..market.snapshot_accessor import SnapshotAccessor
def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class DirectionConsensusResult:
    direction: Optional[Direction]
    ce_score: float = 0.0
    pe_score: float = 0.0
    margin: float = 0.0
    vetoed: bool = False
    veto_reason: str = ""
    sources: dict[str, float] = field(default_factory=dict)


def _add(score_ce: float, score_pe: float, *, ce: float = 0.0, pe: float = 0.0) -> tuple[float, float]:
    return score_ce + ce, score_pe + pe


def resolve_direction_consensus(
    *,
    snap: SnapshotAccessor,
    rule_votes: list[StrategyVote],
    shadow_direction: Direction,
    shadow_score: float,
    ml_direction_hint: Optional[Direction] = None,
    ml_ce_prob: Optional[float] = None,
    regime_signal: Optional[RegimeSignal] = None,
) -> DirectionConsensusResult:
    """Score CE vs PE from multiple sources; veto if margin below threshold or contra-regime."""
    # CLEANUP-BACKLOG (docs/ENGINE_DECISION_FLOW.md §9b): DORMANT for the live
    # profile `trader_master_live_v1` — this function is only reached via the
    # consensus profile (_process_entry_consensus) and the OFF v2 pipeline. Env
    # knobs read below (DIRECTION_CONSENSUS_MIN_MARGIN, DIRECTION_ML_CONFIDENCE_MIN)
    # have NO live effect. To change LIVE direction, edit _process_entry_votes
    # or ml_entry.py:_resolve_direction — NOT here.
    regime_name = regime_signal.regime.value if regime_signal is not None else ""
    # E3-S2: SIDEWAYS regime requires a higher direction margin — flat markets
    # produce noisy signals.  DIRECTION_MIN_MARGIN_SIDEWAYS (default 2.0) overrides
    # the global min_margin when the classifier says SIDEWAYS.
    _sideways_margin = _env_float("DIRECTION_MIN_MARGIN_SIDEWAYS", 2.0)
    _global_margin = _env_float("DIRECTION_CONSENSUS_MIN_MARGIN", 1.25)
    min_margin = _sideways_margin if regime_name == "SIDEWAYS" else _global_margin
    # Direction ML model AUC=0.557 (near coin-flip on holdout) — reduced from 0.35
    # so near-random signal doesn't dominate; rules + shadow score carry the decision.
    # Raise back toward 0.35 only after retraining produces AUC >= 0.65.
    ml_weight = _env_float("DIRECTION_CONSENSUS_ML_WEIGHT", 0.15)
    rule_weight = _env_float("DIRECTION_CONSENSUS_RULE_WEIGHT", 1.0)
    shadow_weight = _env_float("DIRECTION_CONSENSUS_SHADOW_WEIGHT", 1.0)
    momentum_weight = _env_float("DIRECTION_CONSENSUS_MOMENTUM_WEIGHT", 0.75)

    ce_score = 0.0
    pe_score = 0.0
    sources: dict[str, float] = {}

    for vote in rule_votes:
        if vote.strategy_name == "ML_ENTRY":
            continue
        w = rule_weight * float(vote.confidence or 0.5)
        if vote.direction == Direction.CE:
            ce_score += w
            sources[f"rule:{vote.strategy_name}:CE"] = w
        elif vote.direction == Direction.PE:
            pe_score += w
            sources[f"rule:{vote.strategy_name}:PE"] = w

    if shadow_weight > 0 and abs(shadow_score) >= 0.01:
        w = shadow_weight * min(3.0, abs(shadow_score))
        if shadow_direction == Direction.CE:
            ce_score += w
            sources["shadow:CE"] = w
        elif shadow_direction == Direction.PE:
            pe_score += w
            sources["shadow:PE"] = w

    ret5 = snap.fut_return_5m
    if momentum_weight > 0 and ret5 is not None and float(ret5) != 0.0:
        w = momentum_weight
        if float(ret5) > 0:
            ce_score += w
            sources["momentum:CE"] = w
        else:
            pe_score += w
            sources["momentum:PE"] = w

    if ml_weight > 0:
        if ml_ce_prob is not None:
            w_ce = ml_weight * float(ml_ce_prob)
            w_pe = ml_weight * (1.0 - float(ml_ce_prob))
            ce_score += w_ce
            pe_score += w_pe
            sources["direction_ml:CE"] = w_ce
            sources["direction_ml:PE"] = w_pe
        elif ml_direction_hint in (Direction.CE, Direction.PE):
            w = ml_weight * 0.5
            if ml_direction_hint == Direction.CE:
                ce_score += w
                sources["direction_ml_hint:CE"] = w
            else:
                pe_score += w
                sources["direction_ml_hint:PE"] = w

    margin = abs(ce_score - pe_score)
    if ce_score <= 0 and pe_score <= 0:
        return DirectionConsensusResult(
            direction=None,
            ce_score=ce_score,
            pe_score=pe_score,
            margin=margin,
            vetoed=True,
            veto_reason="no_direction_signals",
            sources=sources,
        )
    if margin < min_margin:
        return DirectionConsensusResult(
            direction=None,
            ce_score=ce_score,
            pe_score=pe_score,
            margin=margin,
            vetoed=True,
            veto_reason=f"unclear_margin<{min_margin:g}",
            sources=sources,
        )
    direction = Direction.CE if ce_score > pe_score else Direction.PE

    # ML-confidence gate (consensus mode). Require the direction-ML model to be
    # sufficiently confident in the CHOSEN side, else abstain. OFF by default
    # (DIRECTION_ML_CONFIDENCE_MIN=0). Validated 2026-06-09: bars where the ML
    # model is confident (|p-0.5| large) are 73-77% direction-accurate OOS vs
    # ~53-55% for low-confidence bars — this gate trades the unsure bars away.
    # NOTE: this works in consensus mode, unlike DIRECTION_ML_FILTER_MIN_PROB
    # (which only fires in the standalone direction-ML policy path).
    _ml_conf_min = _env_float("DIRECTION_ML_CONFIDENCE_MIN", 0.0)
    if _ml_conf_min > 0 and ml_ce_prob is not None:
        chosen_ml_prob = float(ml_ce_prob) if direction == Direction.CE else (1.0 - float(ml_ce_prob))
        sources["ml_chosen_prob"] = chosen_ml_prob
        if chosen_ml_prob < _ml_conf_min:
            return DirectionConsensusResult(
                direction=None,
                ce_score=ce_score,
                pe_score=pe_score,
                margin=margin,
                vetoed=True,
                veto_reason=f"ml_confidence<{_ml_conf_min:g}({chosen_ml_prob:.2f})",
                sources=sources,
            )

    # Regime guard — abstain on EXPANSION/event days. Validated 2026-06-09:
    # when the opening range is wide (>= REGIME_GUARD_MAX_ORW, e.g. 0.8%) the
    # direction edge collapses to noise (both ML and structural signals ~50%),
    # because these are gap/event days where price expands directionally and the
    # mean-reversion read breaks. OFF by default (REGIME_GUARD_MAX_ORW=0).
    _max_orw = _env_float("REGIME_GUARD_MAX_ORW", 0.0)
    if _max_orw > 0 and regime_signal is not None:
        _ev = regime_signal.evidence if isinstance(regime_signal.evidence, dict) else {}
        _orw = _ev.get("opening_range_width_pct")
        if isinstance(_orw, (int, float)) and _orw >= _max_orw:
            return DirectionConsensusResult(
                direction=None,
                ce_score=ce_score,
                pe_score=pe_score,
                margin=margin,
                vetoed=True,
                veto_reason=f"expansion_day_orw>={_max_orw:g}({_orw:.4f})",
                sources=sources,
            )

    # Regime-direction conflict veto.
    # If the regime classifier says BREAKOUT or PANIC with a clear bear/bull lean,
    # block a trade whose direction contradicts that lean.  A CE entry during a
    # BREAKOUT_BEAR (bear_score > 0, zero bull evidence) is a signal-vs-regime
    # conflict: the direction signals caught a brief bounce but the structural
    # read is bearish.  Require the caller to pass regime_signal to enable this.
    if regime_signal is not None:
        ev = regime_signal.evidence if isinstance(regime_signal.evidence, dict) else {}
        bear_score = float(ev.get("bear_score") or 0.0)
        bull_score = float(ev.get("bull_score") or 0.0)
        is_breakout = regime_signal.regime == Regime.BREAKOUT
        is_panic    = regime_signal.regime == Regime.PANIC
        contra_ce = direction == Direction.CE and (
            (is_breakout and bear_score > bull_score) or is_panic
        )
        contra_pe = direction == Direction.PE and (
            is_breakout and bull_score > bear_score
        )
        if contra_ce or contra_pe:
            regime_lbl = f"{regime_signal.regime.value}_{'bear' if bear_score >= bull_score else 'bull'}"
            return DirectionConsensusResult(
                direction=None,
                ce_score=ce_score,
                pe_score=pe_score,
                margin=margin,
                vetoed=True,
                veto_reason=f"contra_regime:{regime_lbl} bear={bear_score} bull={bull_score}",
                sources=sources,
            )

    return DirectionConsensusResult(
        direction=direction,
        ce_score=ce_score,
        pe_score=pe_score,
        margin=margin,
        vetoed=False,
        sources=sources,
    )


def ml_entry_timing_only(vote: StrategyVote) -> bool:
    raw = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
    if raw.get("_ml_entry_timing_only"):
        return True
    return os.getenv("ML_ENTRY_DIRECTION_MODE", "").strip().lower() == "consensus"


def extract_ml_direction_hint(vote: StrategyVote) -> tuple[Optional[Direction], Optional[float]]:
    raw = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
    prob = raw.get("ml_direction_ce_prob")
    if prob is not None:
        try:
            p = float(prob)
            return (Direction.CE if p >= 0.5 else Direction.PE, p)
        except (TypeError, ValueError):
            pass
    hint = str(raw.get("ml_direction_hint") or "").strip().upper()
    if hint == "CE":
        return Direction.CE, None
    if hint == "PE":
        return Direction.PE, None
    if vote.direction in (Direction.CE, Direction.PE):
        return vote.direction, None
    return None, None
