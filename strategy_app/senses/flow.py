"""Flow/OFI sense — "is one side being hit?" (board, handover §4).

Order-flow imbalance from depth. Depth is often absent in sim (the handover notes
depth/OFI did NOT help timing — it may help direction), so this sense ABSTAINS
cleanly when the feed is missing rather than guessing. Used by ConflictAnalysis
(price/flow decouple, direction/flow disagreement), never as a standalone trigger.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import SenseVerdict

OFI_BAND = 0.15     # |net_ofi| below this => neutral


class FlowSense:
    name = "flow"

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        net_ofi = context.get("net_ofi")
        if net_ofi is None:
            return SenseVerdict.abstain(self.name, reason="no depth/ofi feed")
        net_ofi = float(net_ofi)
        if net_ofi > OFI_BAND:
            verdict = "bull"
        elif net_ofi < -OFI_BAND:
            verdict = "bear"
        else:
            verdict = "neutral"
        return SenseVerdict(
            sense=self.name,
            verdict=verdict,
            confidence=round(min(1.0, abs(net_ofi)), 3),
            value=net_ofi,
            evidence={
                "net_ofi": net_ofi,
                "ce_bid_strength": context.get("ce_bid_strength"),
                "pe_bid_strength": context.get("pe_bid_strength"),
            },
        )


__all__ = ["FlowSense"]
