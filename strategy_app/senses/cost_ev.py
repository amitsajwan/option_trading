"""Cost/EV sense — "does the expected move pay after cost?" (board B-1.4).

This sense OWNS the option-premium physics (B-2.1 spec §3.1): it maps an
``expected_move_pt`` in the underlying to option-premium % outcomes and the
round-trip cost from ``cost_model.py`` (D4 — no 6 bps anywhere, cost_model only).
OpportunityQuality then merely composes these by an assumed direction accuracy.

⚠ EMPIRICAL-ANCHOR CALIBRATION (B-2.1 open-question #1 — the single biggest e2e P&L
error source; pending per-fill calibration). The right/wrong magnitudes are anchored
to the live findings in the handover §1, NOT a theoretical delta model (which would
ignore exits and be wildly optimistic):

    right-side hold ≈ +3-5%   |   wrong-side hold ≈ -7-8%   |   round-trip cost ≈ 1.3%

The crucial feature is the **ASYMMETRY**: the wrong-side loss is LARGER than the
right-side gain. That is the exit-giveback signature — stops let losers run to the
floor while winners give profit back. It is exactly what makes direction the binding
constraint, and it is what Phase 4 (exit-as-a-sense) must compress: improving exits
raises ``right_pct_at_ref_move`` toward the uncapped delta capture, which lowers the
break-even direction accuracy. Model that Phase-4 improvement by raising
``right_pct_at_ref_move`` (or ``mfe_capture``) here and re-running B-2.6.
"""
from __future__ import annotations

from typing import Any, Mapping

from strategy_app.cost_model import TradingCostModel

from . import SenseVerdict

# Empirical anchors (handover §1, live-trading scale). Both gains scale linearly with
# move size off the mean loaded move; the wrong side is steeper (the exit asymmetry).
REF_MOVE_PT = 117.0            # mean realised move on `loaded` bars (B-0.2 sample)
RIGHT_PCT_AT_REF = 0.04        # +4% premium on a right-side hold at the mean move (post-giveback)
WRONG_PCT_AT_REF = 0.075       # -7.5% premium on a wrong-side hold at the mean move (pre-cap)


class CostEvSense:
    name = "cost_ev"

    def __init__(
        self,
        *,
        cost_model: TradingCostModel | None = None,
        premium_pts: float = 180.0,    # representative ATM option premium (pts == rupees/unit)
        lot_qty: int = 30,             # BankNifty lot (Dhan)
        right_pct_at_ref: float = RIGHT_PCT_AT_REF,
        wrong_pct_at_ref: float = WRONG_PCT_AT_REF,
        ref_move_pt: float = REF_MOVE_PT,
        theta_pct: float = 0.005,      # 10-min theta drag as % of premium
        max_loss_pct: float = 0.08,    # exit floor cap on a wrong-side hold (handover -7 to -8%)
        mfe_capture: float = 1.0,      # Phase-4 lever: >1.0 = exits hold winners better
    ) -> None:
        self.cost_model = cost_model or TradingCostModel()
        self.premium_pts = float(premium_pts)
        self.lot_qty = int(lot_qty)
        self.right_slope = float(right_pct_at_ref) / float(ref_move_pt) * float(mfe_capture)
        self.wrong_slope = float(wrong_pct_at_ref) / float(ref_move_pt)
        self.theta_pct = float(theta_pct)
        self.max_loss_pct = float(max_loss_pct)
        # exposed for the backtest's realised-move accounting
        self.right_at = lambda move: self.right_slope * float(move)
        self.wrong_at = lambda move: -min(self.wrong_slope * float(move) + self.theta_pct, self.max_loss_pct)

    def cost_pct(self) -> float:
        entry_value = self.premium_pts * self.lot_qty
        if entry_value <= 0:
            return 0.0
        bd = self.cost_model.breakdown(entry_value=entry_value, exit_value=entry_value)
        return bd["total_cost_amount"] / entry_value

    # back-compat alias used by the runner
    def _cost_pct(self) -> float:
        return self.cost_pct()

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        expected_move = context.get("expected_move_pt")
        if not expected_move:
            return SenseVerdict.abstain(self.name, reason="no expected_move_pt")
        expected_move = float(expected_move)

        gross_if_right = self.right_at(expected_move)
        gross_if_wrong = self.wrong_at(expected_move)
        cost_pct = self.cost_pct()
        net_5050 = 0.5 * gross_if_right + 0.5 * gross_if_wrong - cost_pct

        return SenseVerdict(
            sense=self.name,
            verdict="+ev" if net_5050 > 0 else "-ev",
            confidence=round(min(1.0, abs(net_5050) / 0.03), 3),
            value=round(net_5050, 5),
            evidence={
                "gross_if_right_pct": round(gross_if_right, 5),
                "gross_if_wrong_pct": round(gross_if_wrong, 5),
                "cost_pct": round(cost_pct, 5),
                "net_after_cost_pct": round(net_5050, 5),
                "asymmetry": round(abs(gross_if_wrong) / gross_if_right, 2) if gross_if_right else None,
                "premium_pts": self.premium_pts, "lot_qty": self.lot_qty,
                "calibration": "empirical-anchor (handover §1; B-2.1 oq#1 — pending per-fill calibration)",
            },
        )


__all__ = ["CostEvSense", "REF_MOVE_PT", "RIGHT_PCT_AT_REF", "WRONG_PCT_AT_REF"]
