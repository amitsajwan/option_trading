"""Destination sense — "is there ROOM for the move to run?" (board B-1.2, the key new gap).

Finds the nearest support/resistance from levels that are ALWAYS present in the raw
snapshot/sim (not the annotation path, which may be empty in sim): OI walls in
``chain_aggregates`` (max_pain, ce/pe top-OI strikes), prior-day high/low, weekly
high/low, and the opening range. Returns the space available vs the expected move, so
the brain can SKIP a loaded spring that has no room to a wall.

``space_to_move_ratio`` = available_space_toward_move / expected_move_pt. The brain
treats a value < 1.0 as "no room" (the move can't complete before hitting a wall).
This sense does not know direction; it reports room on BOTH sides and the worst-case
relevant to a same-size move either way (min of up/down space vs expected move).
"""
from __future__ import annotations

from typing import Any, Mapping

from . import SenseVerdict


class DestinationSense:
    name = "destination"

    def _levels(self, close: float, context: Mapping[str, Any]) -> tuple[list[float], list[float]]:
        """Return (resistances_above, supports_below) from always-present feeds."""
        above: list[float] = []
        below: list[float] = []
        candidates = [
            context.get("max_pain"),
            context.get("ce_oi_top_strike"), context.get("pe_oi_top_strike"),
            context.get("prior_day_high"), context.get("prior_day_low"),
            context.get("week_high"), context.get("week_low"),
            context.get("opening_range_high"), context.get("opening_range_low"),
        ]
        for lvl in candidates:
            if lvl is None:
                continue
            lvl = float(lvl)
            if lvl > close:
                above.append(lvl)
            elif lvl < close:
                below.append(lvl)
        return above, below

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        close = context.get("close")
        expected_move = context.get("expected_move_pt")
        if close is None or not expected_move:
            return SenseVerdict.abstain(self.name, reason="no close or expected_move")
        close = float(close)
        expected_move = float(expected_move)

        above, below = self._levels(close, context)
        if not above and not below:
            return SenseVerdict.abstain(self.name, reason="no levels resolved")

        space_up = (min(above) - close) if above else float("inf")
        space_down = (close - max(below)) if below else float("inf")
        # worst-case room for a move of `expected_move` in either direction
        relevant_space = min(space_up, space_down)
        ratio = relevant_space / expected_move if expected_move > 0 else float("inf")

        verdict = "room" if ratio >= 1.0 else "no_room"
        # confidence: how decisively there is (or isn't) room, capped
        conf = max(0.0, min(1.0, abs(ratio - 1.0)))
        return SenseVerdict(
            sense=self.name,
            verdict=verdict,
            confidence=round(conf, 3),
            value=round(ratio, 3),
            evidence={
                "available_space_up": None if space_up == float("inf") else round(space_up, 1),
                "available_space_down": None if space_down == float("inf") else round(space_down, 1),
                "space_to_move_ratio": round(ratio, 3) if ratio != float("inf") else None,
                "expected_move_pt": expected_move,
                "nearest_resistance": min(above) if above else None,
                "nearest_support": max(below) if below else None,
            },
        )


__all__ = ["DestinationSense"]
