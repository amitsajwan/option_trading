"""Structure sense — the trader's "where are we in the chart?" lens (highs/lows/breakouts).

A trader doesn't just see "a spring is coiled" — they see WHERE it's coiling and whether
price is doing anything: breaking a level (catalyst), failing a break (trap), pinned at the
day's extreme (no room), or coiling mid-range (no catalyst yet). This sense reports that
structure. It is DIRECTION-AGNOSTIC by design: it records breakout up/down as *evidence*
(for the separate direction research), but the verdict the brain acts on is about
*release vs trap vs no-catalyst*, never which side to buy.

Key insight: a **breakout is the real "release"** the Phase-0 proof was missing — the
spring loads (compression + OI), and it releases when price breaks a structural level.
A **fakeout** (broke then snapped back) is a trap to avoid.

Reads these semantic context keys (producers — backtest context builder or the live
adapter/MarketStructureTracker — populate them; this sense stays pure):
  struct_breakout : "up" | "down" | "none"
  struct_fakeout  : bool   (broke a level this/recent bar then reverted back inside)
  struct_swept    : bool   (pierced a prior-day extreme intrabar then closed back inside)
  struct_sweep_direction : "up" | "down" | "none"   (which liquidity pool was taken — EVIDENCE only)
  struct_position : "near_high" | "near_low" | "inside"
  struct_trend    : "up" | "down" | "choppy"   (swing / EMA structure)
  day_high, day_low : floats (evidence)

A liquidity sweep is folded into ``struct_fakeout`` by the producer (a sweep IS a trap), so it
flows through the existing fakeout verdict and the brain's ``loaded_into_fakeout`` conflict.
``struct_sweep_direction`` stays EVIDENCE only — direction-agnostic, like breakout direction.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import SenseVerdict


class StructureSense:
    name = "structure"

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        breakout = context.get("struct_breakout")
        fakeout = bool(context.get("struct_fakeout"))
        position = context.get("struct_position")
        trend = context.get("struct_trend")
        if breakout is None and position is None and trend is None:
            return SenseVerdict.abstain(self.name, reason="no structure inputs")

        evidence = {
            "breakout": breakout, "fakeout": fakeout, "position": position, "trend": trend,
            "swept": bool(context.get("struct_swept")),
            "sweep_direction": context.get("struct_sweep_direction"),
            "day_high": context.get("day_high"), "day_low": context.get("day_low"),
        }

        # Verdict ladder reflects the trader's read, strongest signal first.
        if fakeout:
            # broke a level and snapped back — a trap. Strongest "do not chase" signal.
            return SenseVerdict(self.name, "fakeout", confidence=0.7, value=-1.0, evidence=evidence)
        if breakout in ("up", "down"):
            # the spring is RELEASING through a level — the catalyst. Trend-aligned = cleaner.
            aligned = (breakout == "up" and trend == "up") or (breakout == "down" and trend == "down")
            return SenseVerdict(self.name, "breakout", confidence=0.75 if aligned else 0.55,
                                value=1.0, evidence={**evidence, "trend_aligned": aligned})
        if position in ("near_high", "near_low"):
            # pinned at the day's edge — limited room one way, reversal-prone. Caution.
            return SenseVerdict(self.name, "at_extreme", confidence=0.5, value=0.0, evidence=evidence)
        # inside the range, no break yet — loaded spring still waiting for its catalyst.
        return SenseVerdict(self.name, "coiling", confidence=0.4, value=0.3, evidence=evidence)


__all__ = ["StructureSense"]
