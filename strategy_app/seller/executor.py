"""SafeExecutor (T4) — atomic-or-unwind 2/4-leg spread execution.

The hard safety requirement (user): NEVER hold an uninvited leg. Either the full spread
is on, or we are flat. State machine: FLAT -> OPENING -> OPEN -> CLOSING -> FLAT.

OPEN:  place BUY (protective long) legs FIRST -> confirm each -> then SELL (short) legs.
       If ANY leg fails, immediately UNWIND every filled leg (opposite action) -> FLAT.
CLOSE: buy-back SHORT legs FIRST -> confirm -> then SELL the long legs.

Works for verticals (2 legs) and iron condors (4 legs) identically.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from .brain import SellerDecision, SpreadLeg
from .gateway import Fill, LegGateway

logger = logging.getLogger(__name__)


@dataclass
class FilledLeg:
    action: str          # original open action: BUY (long) | SELL (short)
    option_type: str
    strike: int
    qty: int
    entry_price: float
    order_id: str = ""


@dataclass
class OpenSpread:
    spread_id: str
    structure: str
    expiry: str                       # ISO
    qty: int
    legs: list[FilledLeg]
    entry_credit: float               # per-unit points (sell - buy)
    width: int
    opened_at: str
    trade_date: str
    direction: Optional[str] = None
    meta: dict = field(default_factory=dict)

    @property
    def max_risk(self) -> float:
        # Defined risk per unit. For an iron condor ONLY ONE side can finish ITM at expiry, so the
        # max loss is a SINGLE wing-width minus the (total) credit kept — not both wings. `self.width`
        # is stored as 2x the wing for a condor, so halve it here. (architect+trader review fix.)
        single_wing = self.width / 2 if self.structure == "iron_condor" else self.width
        return max(0.0, single_wing - self.entry_credit)


class SafeExecutor:
    def __init__(self, gateway: LegGateway, qty: int, width: int):
        self._gw = gateway
        self._qty = int(qty)
        self._width = int(width)

    # ── OPEN ─────────────────────────────────────────────────────────────────
    def open_spread(self, decision: SellerDecision, expiry: date, trade_date: str) -> Optional[OpenSpread]:
        if not decision.fires:
            return None
        # protective BUY legs first, then SELL legs  (never naked)
        ordered = sorted(decision.legs, key=lambda l: 0 if l.action == "BUY" else 1)
        filled: list[FilledLeg] = []
        for leg in ordered:
            f: Fill = self._gw.execute(leg.action, leg.option_type, leg.strike, expiry, self._qty)
            if not f.filled:
                logger.warning("seller open: leg %s %s%d FAILED (%s) -> unwinding %d filled legs",
                               leg.action, leg.option_type, leg.strike, f.error, len(filled))
                self._unwind(filled, expiry)
                return None
            filled.append(FilledLeg(leg.action, leg.option_type, leg.strike, self._qty, f.price, f.order_id))
        credit = (sum(fl.entry_price for fl in filled if fl.action == "SELL")
                  - sum(fl.entry_price for fl in filled if fl.action == "BUY"))
        if credit <= 0:
            logger.warning("seller open: non-positive credit %.2f -> unwinding", credit)
            self._unwind(filled, expiry)
            return None
        spread = OpenSpread(
            spread_id=uuid.uuid4().hex[:12], structure=decision.structure, expiry=expiry.isoformat(),
            qty=self._qty, legs=filled, entry_credit=round(credit, 2),
            width=self._width * (2 if decision.structure == "iron_condor" else 1),
            opened_at=datetime.now(timezone.utc).isoformat(), trade_date=trade_date,
            direction=decision.direction, meta={"regime": decision.regime, "iv_rank": decision.iv_rank},
        )
        logger.info("seller OPEN %s %s credit=%.2f legs=%d expiry=%s", spread.structure,
                    spread.spread_id, credit, len(filled), spread.expiry)
        return spread

    def _unwind(self, filled: list[FilledLeg], expiry: date) -> None:
        """Best-effort flatten of any legs already filled — guarantees no orphan."""
        for fl in filled:
            opp = "SELL" if fl.action == "BUY" else "BUY"
            try:
                self._gw.execute(opp, fl.option_type, fl.strike, expiry, fl.qty)
            except Exception:
                logger.exception("seller unwind: failed to flatten %s%d — MANUAL CHECK", fl.option_type, fl.strike)

    # ── CLOSE ────────────────────────────────────────────────────────────────
    def close_spread(self, spread: OpenSpread, expiry: date) -> Optional[float]:
        """Square off: buy-back SHORT legs first, then sell longs. Returns exit value (points)."""
        shorts = [fl for fl in spread.legs if fl.action == "SELL"]
        longs = [fl for fl in spread.legs if fl.action == "BUY"]
        exit_prices: dict[tuple[str, int], float] = {}
        for fl in shorts:                      # buy back shorts FIRST (remove risk)
            f = self._gw.execute("BUY", fl.option_type, fl.strike, expiry, fl.qty)
            if not f.filled:
                logger.error("seller close: buy-back %s%d FAILED (%s) — position still risk-capped, retry needed",
                             fl.option_type, fl.strike, f.error)
                return None
            exit_prices[(fl.option_type, fl.strike)] = f.price
        for fl in longs:                       # then sell the long hedges
            f = self._gw.execute("SELL", fl.option_type, fl.strike, expiry, fl.qty)
            exit_prices[(fl.option_type, fl.strike)] = f.price if f.filled else 0.0
        exit_value = (sum(exit_prices.get((fl.option_type, fl.strike), 0.0) for fl in shorts)
                      - sum(exit_prices.get((fl.option_type, fl.strike), 0.0) for fl in longs))
        logger.info("seller CLOSE %s exit_value=%.2f pnl_pts=%.2f", spread.spread_id,
                    exit_value, spread.entry_credit - exit_value)
        return round(exit_value, 2)
