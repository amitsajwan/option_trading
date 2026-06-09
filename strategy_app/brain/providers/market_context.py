"""MarketContextProvider — surfaces deterministic reference levels to the brain.

Emits ``market.*`` keys (prev-day / recent-week high/low/close/return) into
``DayContext.provider_context`` at morning-briefing time. These are real trading
reference levels in their own right (a Destination sense can use prev-day H/L)
and the grounded facts the LLM morning posture reasons over.

Reliable + free: computed from our own daily history, no external API, no LLM.
Degrades to ``{}`` when the OHLC file is absent (same contract as
DailyFeaturesProvider).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Optional

from ..plugin import ContextProvider
from .market_levels import load_daily_ohlc, prefixed_levels

logger = logging.getLogger(__name__)


class MarketContextProvider(ContextProvider):
    """Reads daily OHLC and surfaces prev-day / recent-week reference levels."""

    name = "market_context"

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else None

    def provide(self, trade_date: date) -> dict[str, Any]:
        try:
            records = load_daily_ohlc(self._path)
            levels = prefixed_levels(records, asof=trade_date)
        except Exception as exc:  # never raise — ContextProvider contract
            logger.warning("market_context failed date=%s error=%s", trade_date, exc)
            return {}
        if not levels:
            logger.debug("market_context no levels for date=%s", trade_date)
        return levels


__all__ = ["MarketContextProvider"]
