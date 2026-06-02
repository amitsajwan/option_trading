"""Entry-pipeline contracts: GateOutcome, GateResult, GateTrace, Gate Protocol,
EntryContext, and the single-loop pipeline runner run_chain().

These types are pure data / structural — no engine imports, no os.getenv calls.
They form the boundary that all gate implementations depend on.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..contracts import Direction, RiskContext, StrategyVote
    from ..market.regime import RegimeSignal
    from ..market.snapshot_accessor import SnapshotAccessor
    from .entry_config import EntryConfig


# ---------------------------------------------------------------------------
# Gate outcome taxonomy
# ---------------------------------------------------------------------------

class GateOutcome(str, Enum):
    PASS = "pass"
    VETO = "veto"                   # whole bar dead — stop pipeline
    SKIP_CANDIDATE = "skip_candidate"  # this vote is dead, try next ranked vote


# ---------------------------------------------------------------------------
# GateResult — the value a gate returns
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    outcome: GateOutcome
    reason: str = ""
    values: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls) -> "GateResult":
        return cls(outcome=GateOutcome.PASS)

    @classmethod
    def veto(cls, reason: str, **values: Any) -> "GateResult":
        return cls(outcome=GateOutcome.VETO, reason=reason, values=dict(values))

    @classmethod
    def skip(cls, reason: str, **values: Any) -> "GateResult":
        return cls(outcome=GateOutcome.SKIP_CANDIDATE, reason=reason, values=dict(values))


# ---------------------------------------------------------------------------
# GateTrace — one entry per gate call, accumulated on EntryContext
# ---------------------------------------------------------------------------

@dataclass
class GateTrace:
    gate_name: str
    outcome: GateOutcome
    reason: str = ""
    values: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Gate Protocol
# ---------------------------------------------------------------------------

class Gate:
    """Base class for pipeline gates.

    Subclasses override :meth:`apply`.  ``name`` is used in trace logs.
    This is intentionally a regular class (not Protocol) so isinstance checks
    and test subclassing work without importing ``typing_extensions``.
    """

    name: str = "unnamed_gate"

    def apply(self, ctx: "EntryContext") -> GateResult:  # pragma: no cover
        raise NotImplementedError(f"{self.__class__.__name__}.apply() not implemented")


# ---------------------------------------------------------------------------
# EntryContext — the shared typed bus for one bar / candidate evaluation
# ---------------------------------------------------------------------------

@dataclass
class EntryContext:
    """Shared context threaded through every gate in the pipeline.

    *Inputs* (set once at construction, never mutated by gates):
        snap, regime, risk, votes, config, decision_id

    *Candidate-scoped fields* (reset per candidate by reset_candidate()):
        candidate, direction, strike, premium, lots

    *Observability*:
        trace — list of GateTrace, appended by run_chain()
    """

    # --- inputs (immutable for the bar) ---
    snap: "SnapshotAccessor"
    regime: "RegimeSignal"
    risk: "RiskContext"
    votes: "list[StrategyVote]"
    config: "EntryConfig"
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    # --- progressively filled by gates (candidate-scoped) ---
    candidate: "Optional[StrategyVote]" = field(default=None)
    direction: "Optional[Direction]" = field(default=None)
    strike: Optional[int] = field(default=None)
    premium: Optional[float] = field(default=None)
    lots: Optional[int] = field(default=None)

    # --- observability ---
    trace: list[GateTrace] = field(default_factory=list)

    def reset_candidate(self, vote: "StrategyVote") -> None:
        """Switch to a new candidate vote; clears candidate-scoped fields."""
        self.candidate = vote
        self.direction = None
        self.strike = None
        self.premium = None
        self.lots = None


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def run_chain(ctx: EntryContext, gates: list[Gate]) -> GateResult:
    """Run gates in order for the current candidate on *ctx*.

    * ``PASS``           → continue to next gate
    * ``VETO``           → append trace, return immediately (bar dead)
    * ``SKIP_CANDIDATE`` → append trace, return immediately (try next vote)

    All non-PASS results are appended to ``ctx.trace``.
    PASS results are also traced for full observability.
    """
    for gate in gates:
        result = gate.apply(ctx)
        ctx.trace.append(
            GateTrace(
                gate_name=gate.name,
                outcome=result.outcome,
                reason=result.reason,
                values=result.values,
            )
        )
        if result.outcome != GateOutcome.PASS:
            return result
    return GateResult.ok()
