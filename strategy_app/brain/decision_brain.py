"""DecisionBrain — Layer 2 of the Intelligent Brain (board B-2.2).

Implements the B-2.1 spec (docs/INTELLIGENT_BRAIN_B2_1_DECISION_LOGIC_SPEC.md):
ConflictAnalysis + OpportunityQuality + the policy ladder. Consumes a per-bar map
of ``{sense_name: SenseVerdict}`` and returns a ``BrainDecision``
(TRADE/WAIT/SKIP/NO_TRADE, side, size=1).

Doctrine baked in (D1): **size is always 1 lot** — selectivity (OpportunityQuality)
is the only risk lever; there is no sizing decision here. Every call returns a
fully-populated decision suitable for a per-bar reasoning trace (D7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from strategy_app.senses import SenseVerdict
from strategy_app.senses.direction import UNKNOWN

# ---- constants (B-2.1 §6; tune only via sim-gated oversight, never auto-live) ----
RET_EPS = 5.0
SPACE_MIN = 1.0
SCORE_MIN = 3
P_REF = 0.55                 # realistic structural-bias direction (Sprint-4 replaces)
EDGE_THRESHOLD = 0.0
QUALITY_MIN = 5
W_EDGE, W_TAIL, W_ROOM, W_STRUCT = 0.45, 0.25, 0.15, 0.15
EDGE_FULL, SPACE_FULL = 0.03, 3.0
CURVE_POINTS = (0.50, 0.55, 0.58, 0.60, 1.0)
# structure as a CONFIRMING VOTE (per operator): a breakout = the spring releasing through a
# level confirms the setup's timing (the "release" Phase-0 lacked) and votes the quality UP;
# a fakeout (failed-breakout trap) withholds confirmation and votes it DOWN. This is a
# conviction/agreement vote, NOT an edge-size claim — on 8 quiet days breakout did not predict
# a bigger move (n=3), so W_STRUCT is modest: it nudges, it does not gate. Direction-agnostic.
_STRUCT_QUALITY = {"breakout": 1.0, "coiling": 0.5, "inside": 0.5, "at_extreme": 0.5, "fakeout": 0.0}
_STRUCT_CONFIRMS = {"breakout"}     # which structure verdicts cast a confirming vote

# Tradeable regimes: a LOADED SPRING is by definition compression (atr_build < 0.7*atr_base),
# which the regime sense labels "compressed" — so it MUST be tradeable. The policy's
# "NOT alive -> NO_TRADE" intent is to block DEAD (no vol) and CHAOTIC (too wild), not the
# coiled state where the edge lives (handover §6 parenthetical: "dead/wrong personality").
_ALIVE_REGIMES = {"alive", "expanding", "compressed"}
_LOADED_VERDICTS = {"loaded", "released"}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass(frozen=True)
class ConflictVerdict:
    any: bool
    conflicts: list[str]
    action: str                     # "" | "WAIT" | "SKIP"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OpportunityVerdict:
    edge_pct: float
    quality: int
    p_ref: float
    net_curve: dict[float, float]
    passes: bool
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrainDecision:
    action: str                     # TRADE | WAIT | SKIP | NO_TRADE
    side: str
    size: int
    reason: str
    ladder_step: int
    verdicts: dict[str, Any] = field(default_factory=dict)
    conflict: ConflictVerdict | None = None
    opportunity: OpportunityVerdict | None = None

    def to_trace(self) -> dict[str, Any]:
        return {
            "action": self.action, "side": self.side, "size": self.size,
            "reason": self.reason, "ladder_step": self.ladder_step,
            "verdicts": {k: (v.to_trace() if isinstance(v, SenseVerdict) else v)
                         for k, v in self.verdicts.items()},
            "conflict": None if self.conflict is None else {
                "any": self.conflict.any, "conflicts": self.conflict.conflicts,
                "action": self.conflict.action, "evidence": self.conflict.evidence},
            "opportunity": None if self.opportunity is None else {
                "edge_pct": self.opportunity.edge_pct, "quality": self.opportunity.quality,
                "p_ref": self.opportunity.p_ref, "net_curve": self.opportunity.net_curve,
                "passes": self.opportunity.passes, "evidence": self.opportunity.evidence},
        }


def analyze_conflicts(verdicts: Mapping[str, SenseVerdict]) -> ConflictVerdict:
    """Layer-2 ConflictAnalysis — compares senses for contradictions (B-2.1 §2)."""
    move = verdicts.get("move")
    direction = verdicts.get("direction")
    flow = verdicts.get("flow")
    destination = verdicts.get("destination")

    fired: list[str] = []
    ev: dict[str, Any] = {}
    move_strong = bool(move and move.verdict in _LOADED_VERDICTS)

    side = direction.verdict if direction else UNKNOWN
    flow_dir = flow.verdict if flow else None

    # A — move strong but direction & flow disagree
    if move_strong and side != UNKNOWN and flow_dir in ("bull", "bear"):
        disagree = (flow_dir == "bull" and side == "PE") or (flow_dir == "bear" and side == "CE")
        if disagree:
            fired.append("move_strong_but_direction_conflicted")
            ev["A"] = {"side": side, "flow": flow_dir}

    # B — order flow and price decoupled
    if flow_dir in ("bull", "bear") and move:
        last_ret = float(move.evidence.get("last_bar_return", 0.0))
        if (flow_dir == "bull" and last_ret < -RET_EPS) or (flow_dir == "bear" and last_ret > RET_EPS):
            fired.append("ofi_bullish_price_falling")
            ev["B"] = {"flow": flow_dir, "last_bar_return": last_ret}

    # C — velocity pop with weak volume
    if move and bool(move.evidence.get("velocity")) and not bool(move.evidence.get("volume")):
        fired.append("velocity_up_volume_weak")
        ev["C"] = {"velocity": True, "volume": False}

    # E — loaded spring releasing into a FAKEOUT (broke a level then snapped back) = a trap.
    structure = verdicts.get("structure")
    if move_strong and structure is not None and structure.verdict == "fakeout":
        fired.append("loaded_into_fakeout")
        ev["E"] = {"structure": "fakeout"}

    # D — loaded but no room (structural; SKIP)
    if move_strong and destination is not None and not destination.is_abstain:
        ratio = destination.evidence.get("space_to_move_ratio")
        if ratio is not None and float(ratio) < SPACE_MIN:
            fired.append("loaded_but_no_space")
            ev["D"] = {"space_to_move_ratio": float(ratio)}

    if not fired:
        return ConflictVerdict(any=False, conflicts=[], action="", evidence={})
    action = "SKIP" if "loaded_but_no_space" in fired else "WAIT"
    return ConflictVerdict(any=True, conflicts=fired, action=action, evidence=ev)


def assess_opportunity(
    verdicts: Mapping[str, SenseVerdict], *, p_ref: float = P_REF, gate_p: float | None = None,
) -> OpportunityVerdict:
    """Layer-2 OpportunityQuality — edge composition + 0..10 rank + gate (B-2.1 §3).

    ``gate_p`` is the direction accuracy at which pass/fail is JUDGED. Live: ``p_ref``
    (the realistic structural bias). In the B-2.6 ``defer_direction`` backtest it is
    1.0 (perfect) — we judge the move/destination/cost setup *as if direction were
    solved*, so the curve can reveal whether direction is the only gap (D5). Gating on
    naive 0.55 here would reject every setup for the deferred-component's sake.
    ``edge_pct`` is always reported at ``p_ref`` for the trace.
    """
    gate_p = p_ref if gate_p is None else gate_p
    cost_ev = verdicts.get("cost_ev")
    move = verdicts.get("move")
    destination = verdicts.get("destination")
    if cost_ev is None or cost_ev.is_abstain or move is None:
        return OpportunityVerdict(edge_pct=0.0, quality=0, p_ref=p_ref,
                                  net_curve={p: 0.0 for p in CURVE_POINTS}, passes=False,
                                  evidence={"reason": "missing cost_ev/move"})

    gr = float(cost_ev.evidence.get("gross_if_right_pct", 0.0))
    gw = float(cost_ev.evidence.get("gross_if_wrong_pct", 0.0))
    cost = float(cost_ev.evidence.get("cost_pct", 0.0))

    def net(p: float) -> float:
        return p * gr + (1.0 - p) * gw - cost

    net_curve = {p: round(net(p), 5) for p in CURVE_POINTS}
    edge = net(p_ref)
    edge_gate = net(gate_p)

    prob_200 = float(move.evidence.get("prob_200", 0.0))
    space_ratio = None
    if destination is not None and not destination.is_abstain:
        space_ratio = destination.evidence.get("space_to_move_ratio")
    space_ratio = float(space_ratio) if space_ratio is not None else 1.0

    structure = verdicts.get("structure")
    struct_state = structure.verdict if (structure is not None and not structure.is_abstain) else "inside"
    n_struct = _STRUCT_QUALITY.get(struct_state, 0.4)

    n_edge = _clamp01(edge_gate / EDGE_FULL)
    n_tail = _clamp01(prob_200 / 0.20)
    n_room = _clamp01((space_ratio - 1.0) / (SPACE_FULL - 1.0))
    q_raw = 10 * (W_EDGE * n_edge + W_TAIL * n_tail + W_ROOM * n_room + W_STRUCT * n_struct)
    quality = round(q_raw)
    structure_confirms = struct_state in _STRUCT_CONFIRMS    # breakout casts the confirming vote
    passes = (edge_gate > EDGE_THRESHOLD) and (quality >= QUALITY_MIN)

    return OpportunityVerdict(
        edge_pct=round(edge, 5), quality=quality, p_ref=p_ref, net_curve=net_curve, passes=passes,
        evidence={"gross_if_right_pct": gr, "gross_if_wrong_pct": gw, "cost_pct": cost,
                  "gate_p": gate_p, "edge_at_gate_p": round(edge_gate, 5),
                  "prob_200": prob_200, "space_to_move_ratio": space_ratio, "structure": struct_state,
                  "structure_confirms": structure_confirms, "quality_raw": round(q_raw, 3),
                  "n_edge": round(n_edge, 3), "n_tail": round(n_tail, 3),
                  "n_room": round(n_room, 3), "n_struct": round(n_struct, 3)},
    )


class DecisionBrain:
    """The per-bar deterministic decision ladder (B-2.1 §4). No LLM, no I/O, <1s."""

    def __init__(self, *, p_ref: float = P_REF, defer_direction: bool = False) -> None:
        self.p_ref = p_ref
        # defer_direction=True bypasses the WAIT-on-UNKNOWN rung — the "direction is
        # the deferred Sprint-4 component" mode used by the B-2.6 cost gate (D5).
        self.defer_direction = defer_direction

    def decide(self, verdicts: Mapping[str, SenseVerdict]) -> BrainDecision:
        snap = dict(verdicts)
        risk = verdicts.get("risk")
        regime = verdicts.get("regime")
        move = verdicts.get("move")
        direction = verdicts.get("direction")
        destination = verdicts.get("destination")
        execution = verdicts.get("execution")

        def decision(action, side, size, reason, step, conflict=None, opp=None):
            return BrainDecision(action=action, side=side, size=size, reason=reason,
                                 ladder_step=step, verdicts=snap, conflict=conflict, opportunity=opp)

        # 0 — risk hard block
        if risk is not None and risk.verdict == "blocked":
            return decision("SKIP", "", 0, "risk_" + "_".join(risk.evidence.get("blocked_reasons", [])), 0)

        # 1 — regime must be alive/expanding
        if regime is None or regime.is_abstain or regime.verdict not in _ALIVE_REGIMES:
            return decision("NO_TRADE", "", 0, f"regime_{regime.verdict if regime else 'missing'}", 1)

        # 2 — a loaded spring must be present (the `loaded` PAIR is authoritative; sum-of-4 retired)
        if move is None or move.is_abstain or move.verdict not in _LOADED_VERDICTS:
            return decision("NO_TRADE", "", 0, "no_loaded_spring", 2)

        # 3 — conflicts
        conflict = analyze_conflicts(verdicts)
        if conflict.any:
            return decision(conflict.action, "", 0, "conflict:" + ",".join(conflict.conflicts), 3, conflict=conflict)

        # 4 — direction (live only; deferred for the B-2.6 gate)
        side = direction.verdict if direction else UNKNOWN
        if not self.defer_direction and side == UNKNOWN:
            return decision("WAIT", "", 0, "direction_unknown", 4, conflict=conflict)

        # 5 — room to destination
        if destination is None or destination.is_abstain:
            return decision("SKIP", "", 0, "no_room_unknown", 5, conflict=conflict)
        ratio = destination.evidence.get("space_to_move_ratio")
        if ratio is None or float(ratio) < SPACE_MIN:
            return decision("SKIP", "", 0, "no_room", 5, conflict=conflict)

        # 6 — opportunity quality / edge (defer mode judges the setup at perfect direction; see D5)
        opp = assess_opportunity(verdicts, p_ref=self.p_ref, gate_p=(1.0 if self.defer_direction else None))
        if not opp.passes:
            return decision("SKIP", "", 0, f"edge_{opp.edge_pct}_q{opp.quality}", 6, conflict=conflict, opp=opp)

        # 7 — execution quality
        if execution is not None and execution.verdict == "degraded":
            return decision("SKIP", "", 0, f"spread_{execution.evidence.get('spread_pct')}", 7, conflict=conflict, opp=opp)

        # 8 — TRADE, size always 1
        trade_side = side if side in ("CE", "PE") else "CE"
        return decision("TRADE", trade_side, 1, "all_agree", 8, conflict=conflict, opp=opp)


__all__ = ["DecisionBrain", "ConflictVerdict", "OpportunityVerdict", "BrainDecision",
           "analyze_conflicts", "assess_opportunity", "P_REF", "CURVE_POINTS"]
