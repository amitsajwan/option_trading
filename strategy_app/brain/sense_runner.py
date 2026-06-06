"""Sense orchestration — runs the Layer-1 senses for one bar into a verdict map.

Shared by the offline backtest (``ops/research/brain_backtest.py``) and the live
shadow path so both produce identical verdict maps from the same context dict.

Order matters only in that Move publishes ``expected_move_pt``, which Destination
and Cost/EV need; everything else is independent. Senses still never import each
other — this Layer-2 runner is what threads Move's output into the others.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..senses import SenseVerdict
from ..senses.cost_ev import CostEvSense
from ..senses.destination import DestinationSense
from ..senses.direction import DirectionSense
from ..senses.flow import FlowSense
from ..senses.move import MoveSense
from ..senses.regime import RegimeSense
from ..senses.risk import RiskSense
from ..senses.structure import StructureSense

_MOVE, _REGIME, _DEST, _RISK, _FLOW, _STRUCT = (
    MoveSense(), RegimeSense(), DestinationSense(), RiskSense(), FlowSense(), StructureSense())


def run_senses(
    context: Mapping[str, Any],
    *,
    cost_ev: CostEvSense | None = None,
    direction_sense: Any | None = None,
) -> dict[str, SenseVerdict]:
    """Evaluate all senses for one bar; returns ``{sense_name: SenseVerdict}``."""
    move = _MOVE.evaluate(context)
    enriched = {**context, "expected_move_pt": move.evidence.get("expected_move_pt")}

    if cost_ev is None:
        prem = context.get("atm_premium")
        cost_ev = CostEvSense(premium_pts=float(prem)) if prem else CostEvSense()
    direction_sense = direction_sense or DirectionSense()

    return {
        "move": move,
        "regime": _REGIME.evaluate(context),
        "destination": _DEST.evaluate(enriched),
        "cost_ev": cost_ev.evaluate(enriched),
        "risk": _RISK.evaluate(context),
        "flow": _FLOW.evaluate(context),
        "structure": _STRUCT.evaluate(context),
        "direction": direction_sense.evaluate(context),
    }


__all__ = ["run_senses"]
