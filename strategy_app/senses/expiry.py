"""Expiry sense — "how does days-to-expiry shape the trade?" (operator ask 2026-06-07).

A trader picks moneyness with the expiry cycle in mind: early in the cycle (high DTE) the
ATM option carries heavy time value — it's EXPENSIVE, so a fixed-point move is a smaller %
gain and the capital outlay is large → lean OTM (cheaper, more leverage on a big move). Near
expiry the ATM is cheap with high gamma but vicious theta → ATM/close is fine but hold short.

This sense reports DTE, an ATM-expensiveness read, and a preferred moneyness the brain (and,
once wired, the live strike selector) can use. Direction-agnostic; pure (reads context).

Context keys: ``days_to_expiry`` (int), optional ``atm_premium`` (pts) and
``affordable_premium`` (cap, default from SMART_STRIKE_MAX_PREMIUM ~ 1300).
"""
from __future__ import annotations

from typing import Any, Mapping

from . import SenseVerdict

NEAR_DTE = 3        # <= this: ATM cheap, high gamma, fast theta
FAR_DTE = 10        # >  this: ATM expensive (early cycle) -> lean OTM
DEFAULT_AFFORDABLE_PREMIUM = 1300.0   # mirrors SMART_STRIKE_MAX_PREMIUM


class ExpirySense:
    name = "expiry"

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        dte = context.get("days_to_expiry")
        if dte is None:
            return SenseVerdict.abstain(self.name, reason="no days_to_expiry")
        dte = int(dte)
        atm_prem = context.get("atm_premium")
        cap = float(context.get("affordable_premium") or DEFAULT_AFFORDABLE_PREMIUM)

        # base preference from DTE (time-value regime)
        if dte <= 1:
            regime, prefer = "expiry_day", "ATM"
        elif dte <= NEAR_DTE:
            regime, prefer = "near", "ATM"
        elif dte <= FAR_DTE:
            regime, prefer = "mid", "ATM"
        else:
            regime, prefer = "far", "OTM"      # early cycle, expensive ATM

        # premium-driven override: if the ATM is unaffordable, lean OTM regardless of DTE
        atm_expensive = atm_prem is not None and float(atm_prem) > cap
        if atm_expensive:
            prefer = "OTM"

        return SenseVerdict(
            sense=self.name,
            verdict=regime,
            confidence=0.6,
            value=float(dte),
            evidence={
                "days_to_expiry": dte,
                "atm_premium": atm_prem,
                "affordable_premium": cap,
                "atm_expensive": atm_expensive,
                "preferred_moneyness": prefer,
                "reason": (f"dte={dte} ({regime})"
                           + (f", ATM {atm_prem:.0f}>{cap:.0f} -> OTM" if atm_expensive else "")),
            },
        )


__all__ = ["ExpirySense"]
