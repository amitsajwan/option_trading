"""LLMContextProvider — stub for future LLM-powered morning briefing.

Today this returns an empty dict and does nothing.

When you're ready to wire in an LLM:
1.  Set ``BRAIN_LLM_ENABLED=true`` in the environment.
2.  Set ``BRAIN_LLM_API_KEY`` (or use an existing secret).
3.  Set ``BRAIN_LLM_MODEL`` (e.g. ``gpt-4o`` or ``claude-sonnet``).
4.  Implement ``_call_llm()`` below — call the LLM with a structured prompt
    that includes today's macro context (VIX level, BankNifty weekly return,
    upcoming events) and parse the response into a structured dict.

The provider contract is deliberately simple: return a flat dict of
named key-value pairs.  The brain merges them into DayContext.provider_context
under the ``llm.`` prefix.

Example future output::

    {
      "llm.day_assessment": "CALM",
      "llm.confidence": 0.78,
      "llm.reasoning": "Low VIX, RBI policy week over, no major events...",
      "llm.suggested_strategies": ["R1S_SHORT_CE", "PBV1_TOP3_THESIS"],
      "llm.risk_notes": "Watch 09:30 opening move for conviction"
    }
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from ..plugin import ContextProvider

logger = logging.getLogger(__name__)

_CONTEXT_PREFIX = "llm."


class LLMContextProvider(ContextProvider):
    """LLM-powered morning briefing provider.

    No-op stub today.  Set BRAIN_LLM_ENABLED=true to activate.
    """

    name = "llm_context"

    def __init__(self) -> None:
        self._enabled = (
            os.getenv("BRAIN_LLM_ENABLED", "false").strip().lower()
            in ("1", "true", "yes")
        )
        self._api_key = os.getenv("BRAIN_LLM_API_KEY", "").strip()
        self._model = os.getenv("BRAIN_LLM_MODEL", "gpt-4o").strip()

    def provide(self, trade_date: date) -> dict[str, Any]:
        if not self._enabled:
            return {}
        if not self._api_key:
            logger.warning(
                "llm_context: BRAIN_LLM_ENABLED=true but BRAIN_LLM_API_KEY not set"
            )
            return {}
        try:
            return self._call_llm(trade_date)
        except Exception as exc:
            logger.warning("llm_context failed date=%s error=%s", trade_date, exc)
            return {}

    def _call_llm(self, trade_date: date) -> dict[str, Any]:
        """Override this method to implement the actual LLM call.

        Suggested prompt structure::

            system: "You are a BankNifty intraday options trader assistant.
                     Analyse today's macro environment and assess the trading
                     conditions.  Respond in JSON."

            user: f"Date: {trade_date}
                    BankNifty weekly return: {weekly_return}%
                    India VIX: {vix_level}
                    Upcoming events: {events}
                    Question: Is today CALM, NEUTRAL, or VOLATILE for short
                    premium strategies?  Confidence 0-1?  Key risks?"

        Parse the JSON response and return a dict prefixed with 'llm.'.
        """
        # Stub — implement when LLM integration is ready
        raise NotImplementedError(
            "LLMContextProvider._call_llm() not implemented. "
            "Subclass LLMContextProvider and override _call_llm()."
        )


__all__ = ["LLMContextProvider"]
