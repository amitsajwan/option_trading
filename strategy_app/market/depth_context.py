"""Depth context — best-bid/ask data for ATM CE and PE, injected as a side-channel.

Depth data only flows in live mode (Kite WebSocket or REST polling). Historical
replays produce no depth — all callers must treat DepthContext as optional and
fall back to proxy signals (IV fade, VWAP reclaim) when it is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class StrikeDepth:
    """Best bid/ask + top-of-book quantities for one option side (CE or PE)."""

    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_qty: Optional[int]
    ask_qty: Optional[int]
    instrument: str = ""
    fetched_at: str = ""

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def relative_spread(self) -> Optional[float]:
        """Spread as fraction of best_bid (0.005 = 0.5%)."""
        s = self.spread
        if s is None or not self.best_bid or self.best_bid <= 0:
            return None
        return s / self.best_bid

    @property
    def is_valid(self) -> bool:
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.bid_qty is not None
            and self.ask_qty is not None
            and self.bid_qty >= 0
            and self.ask_qty >= 0
        )


@dataclass
class DepthContext:
    """ATM CE + PE depth, available in live mode only.

    When depth is absent (replay / paper without polling) both fields are None
    and ``is_available`` returns False. Strategy code must never require depth.
    """

    ce: Optional[StrikeDepth] = None
    pe: Optional[StrikeDepth] = None

    @property
    def is_available(self) -> bool:
        return self.ce is not None or self.pe is not None

    @property
    def ce_valid(self) -> bool:
        return self.ce is not None and self.ce.is_valid

    @property
    def pe_valid(self) -> bool:
        return self.pe is not None and self.pe.is_valid
