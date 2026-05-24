"""Aggregate CE/PE direction hints; veto when side is unclear.

ML entry timing is separate (ML_ENTRY vote). Direction comes from rule strategies,
shadow/momentum hints, and optional direction-ML as a *weak* vote — not a dictator.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ..contracts import Direction, StrategyVote
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
) -> DirectionConsensusResult:
    """Score CE vs PE from multiple sources; veto if margin below threshold."""
    min_margin = _env_float("DIRECTION_CONSENSUS_MIN_MARGIN", 1.25)
    ml_weight = _env_float("DIRECTION_CONSENSUS_ML_WEIGHT", 0.35)
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
