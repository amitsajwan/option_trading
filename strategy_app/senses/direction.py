"""Direction sense — "which way, or UNKNOWN?" (board B-3.x — Sprint 4).

Direction is built LAST and abstains by default (D5). The real structural-direction
sense (VWAP / OFI / CE-PE depth) is Sprint 4; until then this sense returns UNKNOWN
so the live brain WAITs rather than trades blind.

For the B-2.6 cost-aware gate, direction is the DEFERRED component represented by an
assumed-accuracy parameter ``p`` in the backtest, NOT a live side. Use
:class:`PlaceholderDirection` there: it always supplies a (structural-bias) side so
the ladder proceeds to TRADE, and the backtest sweeps how often that side is right.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import SenseVerdict

UNKNOWN = "UNKNOWN"


class DirectionSense:
    """Live default: abstain to UNKNOWN until the Sprint-4 structural sense ships."""

    name = "direction"

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        return SenseVerdict(
            sense=self.name,
            verdict=UNKNOWN,
            confidence=0.0,
            value=None,
            evidence={"side": UNKNOWN, "basis": [], "reason": "structural direction not built (Sprint 4)"},
        )


class PlaceholderDirection:
    """Backtest-only: a fixed structural-bias side so the ladder reaches TRADE.

    The *correctness* of this side is not modelled here — it is swept as the
    direction-accuracy parameter ``p`` in the B-2.6 backtest (D5). ``confidence`` is
    set above any UNKNOWN threshold purely so the WAIT-on-UNKNOWN rung is bypassed,
    which is exactly the "direction deferred to Sprint 4" semantics.
    """

    name = "direction"

    def __init__(self, side: str = "CE") -> None:
        if side not in ("CE", "PE"):
            raise ValueError("PlaceholderDirection side must be CE or PE")
        self.side = side

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        return SenseVerdict(
            sense=self.name,
            verdict=self.side,
            confidence=1.0,
            value=None,
            evidence={"side": self.side, "basis": ["structural_bias_placeholder"],
                      "note": "accuracy swept as p in B-2.6 — not a real direction call"},
        )


__all__ = ["DirectionSense", "PlaceholderDirection", "UNKNOWN"]
