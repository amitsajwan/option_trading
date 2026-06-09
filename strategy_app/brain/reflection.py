"""Deterministic post-trade reflection — the mechanical trade journal (Phase 2).

This is the *fast, free, falsifiable* half of the brain's "thinking" faculty:
when a position closes we mechanically classify a **loss** into one of a few
buckets and run an **execution-quality** check — with **no LLM**. Most losses
have an obvious cause (we gave back a winner, we were on the wrong side, costs
ate a flat scalp); those we tag deterministically here. Only the genuinely
*ambiguous* losers are marked ``needs_reasoning=True`` and handed to the slow-lane
LLM autopsy later (Phase 3). See
docs/INTELLIGENT_BRAIN_AGENTIC_IMPLEMENTATION_PLAN.md §Phase 2 and principle P1.

Pure module: no I/O, no engine imports, deterministic for a fixed input — so it
is trivially unit-tested and safe to call from anywhere. The
``ClosedTrade.from_position`` adapter maps a live ``PositionContext`` in by
duck-typing, so wiring into the engine later needs no change here.

Units: all P&L / excursion / target / stop values are **signed fractions**
(0.20 == +20%), consistent with ``PositionContext.pnl_pct`` /
``ExitConfig.*_pct / 100``. ``target_frac`` / ``stop_frac`` are positive
magnitudes; ``mae_frac`` is <= 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

# ── tunables (documented; the only knobs) ────────────────────────────────────
EXIT_MISS_MFE_FRAC = 0.80       # reached >=80% of target then lost => we had it
DIRECTION_MISS_MFE_FRAC = 0.25  # never got >25% of target our way => wrong side
NOISE_LOSS_STOP_FRAC = 0.20     # |net loss| <= 20% of stop distance => noise
LOW_CONF = 0.45                 # entry sense below this == "marginal"
HIGH_COST_TO_EDGE = 0.50        # costs >= 50% of edge == overpaid


class LossTag(str, Enum):
    """Deterministic cause-of-loss buckets (mirrors the LLM autopsy enum)."""

    COST_MISS = "cost_miss"            # price made money; costs flipped it negative
    EXIT_MISS = "exit_miss"            # reached most of target, gave it back
    DIRECTION_MISS = "direction_miss"  # wrong side from the start
    ENTRY_MISS = "entry_miss"          # marginal senses at entry; never worked
    NOISE = "noise"                    # small loss, no clear lesson


# ─────────────────────────────────────────────────────────────────────────────
def _verdict_view(v: Any) -> tuple[str, float, bool]:
    """Normalise a SenseVerdict or a trace dict to (verdict, confidence, abstain)."""
    if v is None:
        return ("", 0.0, True)
    if isinstance(v, Mapping):
        verdict = str(v.get("verdict", "")).strip()
        conf = v.get("confidence")
        conf_f = 0.0 if conf is None else float(conf)
        abstain = (not verdict) or verdict.lower() == "unclear" or conf is None
        return (verdict, conf_f, abstain)
    # duck-typed SenseVerdict
    verdict = str(getattr(v, "verdict", "") or "").strip()
    conf_f = float(getattr(v, "confidence", 0.0) or 0.0)
    abstain = bool(getattr(v, "is_abstain", not verdict))
    return (verdict, conf_f, abstain)


@dataclass(frozen=True)
class ClosedTrade:
    """Everything the deterministic autopsy needs about one closed position."""

    direction: str                                   # "CE" | "PE"
    net_pnl_frac: float                              # realised, after costs (signed)
    cost_frac: float                                 # round-trip slippage+charges (>=0)
    mfe_frac: float                                  # max favourable excursion (>=0)
    target_frac: float                               # take-profit magnitude (>0)
    stop_frac: float                                 # stop magnitude (>0)
    mae_frac: float = 0.0                            # max adverse excursion (<=0)
    bars_held: int = 0
    exit_reason: str = ""
    entry_verdicts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def gross_pnl_frac(self) -> float:
        """Price-only P&L, before costs."""
        return self.net_pnl_frac + abs(self.cost_frac)

    @property
    def is_loss(self) -> bool:
        return self.net_pnl_frac < 0.0

    @classmethod
    def from_position(
        cls,
        position: Any,
        *,
        cost_frac: float,
        target_frac: float,
        stop_frac: float,
        entry_verdicts: Optional[Mapping[str, Any]] = None,
        exit_reason: str = "",
    ) -> "ClosedTrade":
        """Adapter from a live ``PositionContext`` (duck-typed — no import)."""
        return cls(
            direction=str(getattr(position, "direction", "") or "").strip().upper(),
            net_pnl_frac=float(getattr(position, "pnl_pct", 0.0) or 0.0),
            cost_frac=abs(float(cost_frac or 0.0)),
            mfe_frac=float(getattr(position, "mfe_pct", 0.0) or 0.0),
            target_frac=abs(float(target_frac or 0.0)),
            stop_frac=abs(float(stop_frac or 0.0)),
            mae_frac=float(getattr(position, "mae_pct", 0.0) or 0.0),
            bars_held=int(getattr(position, "bars_held", 0) or 0),
            exit_reason=str(exit_reason or getattr(position, "exit_reason", "") or "").strip(),
            entry_verdicts=dict(entry_verdicts or {}),
        )


@dataclass(frozen=True)
class AutopsyResult:
    """Deterministic verdict on a closed trade."""

    is_loss: bool
    tag: Optional[str]          # LossTag value, or None for a win/flat
    needs_reasoning: bool       # True => ambiguous, hand to the LLM autopsy (Phase 3)
    evidence: dict[str, Any]


def autopsy(trade: ClosedTrade) -> AutopsyResult:
    """Classify *why* a trade lost — mechanically, where the cause is clear.

    Order matters: cost flip → gave-back-winner → wrong-side → bad-selection →
    noise. Anything that doesn't fit cleanly and isn't trivially small is left
    ``needs_reasoning=True`` for the slow-lane LLM (principle P1).
    """
    net, gross, cost = trade.net_pnl_frac, trade.gross_pnl_frac, abs(trade.cost_frac)
    target = max(trade.target_frac, 0.0)
    stop = max(trade.stop_frac, 0.0)
    mfe = max(trade.mfe_frac, 0.0)
    mfe_to_target = (mfe / target) if target > 0 else None

    has_move = "move" in trade.entry_verdicts
    has_dir = "direction" in trade.entry_verdicts
    move_v, move_c, move_abstain = _verdict_view(trade.entry_verdicts.get("move"))
    dir_v, dir_c, dir_abstain = _verdict_view(trade.entry_verdicts.get("direction"))
    conflict_v, _conflict_c, conflict_abstain = _verdict_view(
        trade.entry_verdicts.get("conflict")
    )
    conflict_present = (not conflict_abstain) and conflict_v.lower() not in ("", "none", "clear")
    # "Marginal" requires *evidence* the entry was weak — a verdict that was
    # actually captured and is abstained/low-confidence, or a conflict. ABSENT
    # verdicts are UNKNOWN, not marginal: otherwise every loss with no captured
    # verdicts is mis-tagged entry_miss and the LLM handoff is starved. A deferred
    # (abstained) direction is by-design for big-move-first, so it is NOT marginal.
    move_marginal = has_move and (move_abstain or move_c < LOW_CONF)
    dir_marginal = has_dir and (not dir_abstain) and dir_c < LOW_CONF
    marginal_entry = move_marginal or dir_marginal or conflict_present

    evidence: dict[str, Any] = {
        "net_pnl_frac": round(net, 6),
        "gross_pnl_frac": round(gross, 6),
        "cost_frac": round(cost, 6),
        "mfe_frac": round(mfe, 6),
        "mae_frac": round(trade.mae_frac, 6),
        "target_frac": round(target, 6),
        "stop_frac": round(stop, 6),
        "mfe_to_target": (round(mfe_to_target, 4) if mfe_to_target is not None else None),
        "giveback_frac": round(max(mfe - net, 0.0), 6),
        "exit_reason": trade.exit_reason,
        "bars_held": trade.bars_held,
        "entry_move_conf": round(move_c, 4),
        "entry_direction_conf": round(dir_c, 4),
        "conflict_present": conflict_present,
        "marginal_entry": marginal_entry,
    }

    # Wins / flats: no loss autopsy (giveback still recorded in evidence).
    if not trade.is_loss:
        return AutopsyResult(is_loss=False, tag=None, needs_reasoning=False, evidence=evidence)

    # 1. Cost flip — price didn't lose, costs did (the "perceived win, real loss").
    if gross >= 0.0 and net < 0.0:
        return AutopsyResult(True, LossTag.COST_MISS.value, False, evidence)

    # 2. Gave back a winner — reached most of target then exited red.
    if mfe_to_target is not None and mfe_to_target >= EXIT_MISS_MFE_FRAC:
        return AutopsyResult(True, LossTag.EXIT_MISS.value, False, evidence)

    # 3. Wrong side from the start — barely went our way, adverse excursion.
    if mfe_to_target is not None and mfe_to_target <= DIRECTION_MISS_MFE_FRAC:
        return AutopsyResult(True, LossTag.DIRECTION_MISS.value, False, evidence)

    # 4. Bad selection — senses were marginal at entry and it never worked.
    if marginal_entry:
        return AutopsyResult(True, LossTag.ENTRY_MISS.value, False, evidence)

    # 5. Noise vs ambiguous — small loss = no lesson; otherwise ask the LLM.
    small = stop > 0 and abs(net) <= NOISE_LOSS_STOP_FRAC * stop
    return AutopsyResult(
        True,
        LossTag.NOISE.value,
        needs_reasoning=not small,
        evidence=evidence,
    )


@dataclass(frozen=True)
class ExecQualityResult:
    """Did execution cost eat the edge that justified the trade?"""

    flag: str                       # "ok" | "high_cost" | "cost_exceeds_edge"
    cost_frac: float
    edge_frac: float
    cost_to_edge: Optional[float]
    overpaid: bool
    evidence: dict[str, Any]


def execution_quality(
    *,
    slippage_frac: float,
    charges_frac: float,
    edge_frac: float,
    high_cost_to_edge: float = HIGH_COST_TO_EDGE,
) -> ExecQualityResult:
    """Compare realised round-trip cost against the expected edge.

    Directly targets the "perceived +0.6% was really −₹46 after slippage+charges"
    failure: a trade can be a price win and a cost loss.
    """
    cost = abs(float(slippage_frac)) + abs(float(charges_frac))
    edge = float(edge_frac)
    if edge <= 0:
        ratio: Optional[float] = None
        flag = "cost_exceeds_edge" if cost > 0 else "ok"
        overpaid = cost > 0
    else:
        ratio = cost / edge
        if ratio >= 1.0:
            flag = "cost_exceeds_edge"
        elif ratio >= high_cost_to_edge:
            flag = "high_cost"
        else:
            flag = "ok"
        overpaid = ratio >= high_cost_to_edge
    return ExecQualityResult(
        flag=flag,
        cost_frac=round(cost, 6),
        edge_frac=round(edge, 6),
        cost_to_edge=(round(ratio, 4) if ratio is not None else None),
        overpaid=overpaid,
        evidence={
            "slippage_frac": round(abs(float(slippage_frac)), 6),
            "charges_frac": round(abs(float(charges_frac)), 6),
        },
    )


def reflect(trade: ClosedTrade, *, edge_frac: float) -> dict[str, Any]:
    """One-call journal record for a closed trade: autopsy + execution quality.

    ``edge_frac`` is the expected edge that justified the entry (e.g. the
    OpportunityQuality net edge). Returns a JSON-safe dict ready to attach to the
    trade's trace and to hand to the slow-lane LLM autopsy (Phase 3) **only when**
    ``autopsy.needs_reasoning`` is True.
    """
    a = autopsy(trade)
    e = execution_quality(
        slippage_frac=abs(trade.cost_frac), charges_frac=0.0, edge_frac=edge_frac
    )
    return {
        "autopsy": {
            "is_loss": a.is_loss,
            "tag": a.tag,
            "needs_reasoning": a.needs_reasoning,
            "evidence": a.evidence,
        },
        "execution": {
            "flag": e.flag,
            "overpaid": e.overpaid,
            "cost_to_edge": e.cost_to_edge,
            "cost_frac": e.cost_frac,
            "edge_frac": e.edge_frac,
        },
    }


__all__ = [
    "LossTag",
    "ClosedTrade",
    "AutopsyResult",
    "autopsy",
    "ExecQualityResult",
    "execution_quality",
    "reflect",
]
