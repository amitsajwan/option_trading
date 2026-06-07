"""Risk sense — "are we allowed to trade?" (board B-1.4).

Pure read of a small risk state (daily drawdown, consecutive losses, in-position).
In live this wraps ``position/tracker.py``; here it reads the same fields from the
context so the brain can hard-block before considering any opportunity.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import SenseVerdict

MAX_DAILY_DD = -0.06        # halt the day at -6% (cf. the 2026-06-05 hardstop lesson)
MAX_CONSEC_LOSSES = 3


class RiskSense:
    name = "risk"

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        daily_dd = float(context.get("daily_dd") or 0.0)
        consec = int(context.get("consec_losses") or 0)
        in_position = bool(context.get("in_position") or False)

        blocked_reasons = []
        if daily_dd <= MAX_DAILY_DD:
            blocked_reasons.append("daily_dd")
        if consec >= MAX_CONSEC_LOSSES:
            blocked_reasons.append("consec_losses")
        if in_position:
            blocked_reasons.append("in_position")

        ok = not blocked_reasons
        return SenseVerdict(
            sense=self.name,
            verdict="ok" if ok else "blocked",
            confidence=1.0,
            value=daily_dd,
            evidence={
                "daily_dd": daily_dd, "consec_losses": consec, "in_position": in_position,
                "blocked_reasons": blocked_reasons,
            },
        )


__all__ = ["RiskSense"]
