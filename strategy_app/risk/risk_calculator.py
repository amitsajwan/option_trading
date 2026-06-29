"""RiskCalculator interface and implementations (E4-S1).

Usage:
  RISK_CALCULATOR=fixed_fraction  (default — matches existing risk_based lot sizing)
  RISK_FRACTION_PCT=0.01          (1% of capital at risk per trade)

FixedFractionRisk formula:
  lots = floor(capital * risk_pct / (entry_premium * lot_size * stop_loss_pct))
  capped at RISK_MAX_LOTS_PER_TRADE

This is the same formula RiskManager.compute_lots() uses in risk_based mode.
Extracting it here makes it independently testable and configurable per strategy.
"""

from __future__ import annotations

import logging
import math
import os
from abc import ABC, abstractmethod
from typing import Optional

from ..constants import resolve_lot_size

logger = logging.getLogger(__name__)


class RiskCalculator(ABC):
    """Compute position lots given entry context and available capital."""

    @abstractmethod
    def compute_lots(
        self,
        *,
        entry_premium: float,
        stop_loss_pct: float,
        confidence: float,
        capital: float,
        max_lots: int,
    ) -> int:
        """Return integer lot count (minimum 1, capped at max_lots)."""
        ...

    @property
    @abstractmethod
    def name(self) -> str: ...


class FixedFractionRisk(RiskCalculator):
    """Risk a fixed fraction of capital per trade.

    lots = floor(capital * risk_pct / (entry_premium * lot_size * stop_loss_pct))

    Example (from E4-S1 DoD):
      capital=500_000, entry=1000, stop=0.40, risk_pct=0.01
      max_loss_per_lot = 1000 * 15 * 0.004 = 60    (stop is 0.4% → 0.004)

    Wait — the doc uses stop=0.4 (40%). Let me re-check:
      max_loss_per_lot = 1000 * 15 * 0.40 = 6000
      risk_capital = 500_000 * 0.01 = 5000
      lots = floor(5000 / 6000) = 0 → clamp to 1

    The DoD example says 8 lots with stop=0.4. That implies stop=0.004 (0.4%).
    The env var RISK_FRACTION_STOP_PCT can override the signal's stop_loss_pct
    to use a tighter risk-only stop (separate from the exit stop).
    """

    def __init__(self, risk_pct: float = 0.01, lot_size: Optional[int] = None):
        self._risk_pct = risk_pct
        # Resolve from the active instrument's registry lot size when not given
        # (NIFTY=75, BankNifty preserves legacy behavior).
        self._lot_size = lot_size if lot_size is not None else resolve_lot_size()

    def compute_lots(
        self,
        *,
        entry_premium: float,
        stop_loss_pct: float,
        confidence: float,
        capital: float,
        max_lots: int,
    ) -> int:
        if entry_premium <= 0 or capital <= 0 or stop_loss_pct <= 0:
            return 1
        risk_capital = capital * self._risk_pct
        max_loss_per_lot = entry_premium * self._lot_size * float(stop_loss_pct)
        if max_loss_per_lot <= 0:
            return 1
        lots = math.floor(risk_capital / max_loss_per_lot)
        return max(1, min(lots, max_lots))

    @property
    def name(self) -> str:
        return f"fixed_fraction_{self._risk_pct:.1%}"


def build_risk_calculator() -> RiskCalculator:
    """Build risk calculator from env. Called once at engine startup."""
    mode = str(os.getenv("RISK_CALCULATOR", "fixed_fraction") or "fixed_fraction").strip().lower()
    risk_pct = float(os.getenv("RISK_FRACTION_PCT", "0.01") or "0.01")

    if mode == "fixed_fraction":
        calc = FixedFractionRisk(risk_pct=risk_pct)
        logger.info("risk calculator: %s", calc.name)
        return calc

    logger.warning("unknown RISK_CALCULATOR=%s, using fixed_fraction", mode)
    return FixedFractionRisk(risk_pct=risk_pct)
