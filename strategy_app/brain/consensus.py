"""Consensus gate — require N-of-M strategy agreement before entering.

Today the StrategyRouter takes the first eligible vote.  ConsensusGate
wraps that step: it collects all ENTRY votes and requires at least
*min_agreeing* of them to point in the same direction before allowing
an entry.  Direction conflict is resolved by majority; ties block.

Configuration
-------------
BRAIN_CONSENSUS_MIN_AGREEING (env int, default 1)
    Minimum number of strategies that must agree on the same direction.
    1  = original behaviour (any single vote passes).
    2  = require corroboration — two strategies must agree.
    0  = disabled (always allow, used in replay/debug mode).

BRAIN_CONSENSUS_REQUIRE_DIRECTION (env bool, default false)
    If true, mixed CE/PE votes always block even if min_agreeing is met
    by the larger side.  Useful when strategies have well-defined setups.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from ..contracts import Direction, StrategyVote

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConsensusResult:
    allowed: bool
    agreed_direction: Optional[str]
    agreeing_count: int
    total_count: int
    reason: str
    agreeing_strategies: tuple[str, ...] = field(default_factory=tuple)


class ConsensusGate:
    """Multi-strategy agreement gate.

    Designed to be extended: subclass and override ``_score_vote`` to
    incorporate LLM sentiment, news signal, or any other external source
    as a synthetic "vote" in the consensus pool.
    """

    def __init__(
        self,
        *,
        min_agreeing: int = 1,
        require_direction_agreement: bool = False,
    ) -> None:
        self._min_agreeing = max(0, int(min_agreeing))
        self._require_direction_agreement = bool(require_direction_agreement)

    @classmethod
    def from_env(cls) -> "ConsensusGate":
        min_agreeing = max(0, int(os.getenv("BRAIN_CONSENSUS_MIN_AGREEING", "1") or 1))
        require_dir = (os.getenv("BRAIN_CONSENSUS_REQUIRE_DIRECTION", "false") or "").strip().lower() in ("1", "true", "yes")
        return cls(min_agreeing=min_agreeing, require_direction_agreement=require_dir)

    @property
    def min_agreeing(self) -> int:
        return self._min_agreeing

    def evaluate(self, entry_votes: list[StrategyVote]) -> ConsensusResult:
        """Evaluate whether the vote pool reaches consensus.

        Only ENTRY votes for CE or PE are considered.  AVOID and other
        signals are handled upstream by the engine.
        """
        if self._min_agreeing == 0:
            return ConsensusResult(
                allowed=True,
                agreed_direction=None,
                agreeing_count=len(entry_votes),
                total_count=len(entry_votes),
                reason="consensus_disabled",
            )

        ce_votes = [v for v in entry_votes if v.direction == Direction.CE]
        pe_votes = [v for v in entry_votes if v.direction == Direction.PE]
        total = len(ce_votes) + len(pe_votes)

        if total == 0:
            return ConsensusResult(
                allowed=False,
                agreed_direction=None,
                agreeing_count=0,
                total_count=0,
                reason="no_entry_votes",
            )

        has_conflict = bool(ce_votes and pe_votes)

        if has_conflict and self._require_direction_agreement:
            return ConsensusResult(
                allowed=False,
                agreed_direction=None,
                agreeing_count=0,
                total_count=total,
                reason="direction_conflict_blocked",
            )

        if len(ce_votes) >= len(pe_votes):
            winning_votes = ce_votes
            winning_dir = "CE"
        else:
            winning_votes = pe_votes
            winning_dir = "PE"

        count = len(winning_votes)
        names = tuple(str(v.strategy_name or "") for v in winning_votes)

        if count < self._min_agreeing:
            reason = (
                f"consensus_not_met:{count}<{self._min_agreeing}_required"
                + ("_direction_conflict" if has_conflict else "")
            )
            logger.debug(
                "consensus gate blocked dir=%s agreeing=%d required=%d",
                winning_dir,
                count,
                self._min_agreeing,
            )
            return ConsensusResult(
                allowed=False,
                agreed_direction=winning_dir,
                agreeing_count=count,
                total_count=total,
                reason=reason,
                agreeing_strategies=names,
            )

        reason = f"consensus_met:{count}_agreeing"
        if has_conflict:
            reason += "_with_minority_conflict"
        logger.debug(
            "consensus gate passed dir=%s agreeing=%d/%d",
            winning_dir,
            count,
            total,
        )
        return ConsensusResult(
            allowed=True,
            agreed_direction=winning_dir,
            agreeing_count=count,
            total_count=total,
            reason=reason,
            agreeing_strategies=names,
        )


__all__ = ["ConsensusGate", "ConsensusResult"]
