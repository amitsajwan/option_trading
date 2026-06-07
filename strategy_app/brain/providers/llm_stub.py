"""LLMContextProvider — LLM-powered morning posture (Layer-3 oversight).

This is the **only** place an LLM touches the trading system, by design.  It
runs once per day at morning-briefing time — OFF the per-bar hot path — and its
output is advisory: the TradingBrain treats ``llm.day_assessment`` as one hint
among several (see ``brain._synthesise_day_score``).  It never places trades and
never sizes; selectivity stays deterministic.

It targets any **OpenAI-compatible** chat endpoint, so you can point it at a
free provider by setting three env vars.  Defaults to Groq's free tier.

Safety contract (matches ``ContextProvider``):
- Disabled by default — returns ``{}`` unless ``BRAIN_LLM_ENABLED=true``.
- **Never raises.**  Any network / timeout / parse failure degrades to ``{}``
  and the brain falls back to its deterministic daily-feature scoring.
- Bounded by ``BRAIN_LLM_TIMEOUT_S`` so a slow free endpoint can't stall the
  morning briefing.

Configuration (env)
-------------------
====================== ====== ============== ====================================
Var                    type   default        meaning
====================== ====== ============== ====================================
BRAIN_LLM_ENABLED      bool   false          master switch
BRAIN_LLM_API_KEY      str    —              provider API key
BRAIN_LLM_BASE_URL     str    Groq           OpenAI-compatible base URL (below)
BRAIN_LLM_MODEL        str    llama-3.3-70b  model slug for the chosen provider
BRAIN_LLM_TIMEOUT_S    float  20             hard wall-clock cap
BRAIN_LLM_MAX_TOKENS   int    512            output cap
BRAIN_LLM_TEMPERATURE  float  0.2            sampling temperature
BRAIN_LLM_JSON_MODE    bool   true           request response_format=json_object
BRAIN_LLM_FEATURES_PATH str   —              daily_regime_features.json to ground
                                             the prompt (else DailyFeatures default)
====================== ====== ============== ====================================

Free OpenAI-compatible providers (set BASE_URL + MODEL + API_KEY):
- Groq      ``https://api.groq.com/openai/v1``                      ``llama-3.3-70b-versatile``
- Gemini    ``https://generativelanguage.googleapis.com/v1beta/openai``  ``gemini-2.0-flash``
- DeepSeek  ``https://api.deepseek.com``                            ``deepseek-chat`` (data → CN)
- xAI Grok  ``https://api.x.ai/v1``                                 ``grok-3-mini``
- OpenRouter ``https://openrouter.ai/api/v1``                       ``...:free`` variants

See ``docs/INTELLIGENT_BRAIN_LLM_OVERSIGHT.md`` for the model comparison.

Output keys (merged into ``DayContext.provider_context``)
---------------------------------------------------------
- ``llm.day_assessment`` — one of ``CALM|NEUTRAL|VOLATILE|AVOID`` (dropped if the
  model returns anything else, keeping ``_synthesise_day_score`` safe)
- ``llm.confidence``     — float clamped to 0..1
- ``llm.reasoning``      — short rationale (audit/trace only)
- ``llm.risk_notes``     — short risk note (audit/trace only)
- ``llm.model``          — which model answered (audit)
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

from ..plugin import ContextProvider
from .daily_features import DailyFeaturesProvider
from .openai_compatible import LLMClientError, chat_completion, extract_json_object

logger = logging.getLogger(__name__)

_CONTEXT_PREFIX = "llm."
_DAILY_PREFIX = "daily."
_VALID_ASSESSMENTS = {"CALM", "NEUTRAL", "VOLATILE", "AVOID"}

_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.3-70b-versatile"

_SYSTEM_PROMPT = (
    "You are a risk-aware assistant for a BankNifty intraday options strategy that "
    "tries to capture large (>=100 point) index moves on a ~10-minute horizon. "
    "Given the date and quantitative context, classify today's trading conditions. "
    "Respond with ONLY a JSON object of the form: "
    '{"day_assessment": "CALM|NEUTRAL|VOLATILE|AVOID", "confidence": 0.0-1.0, '
    '"reasoning": "<=1 sentence", "risk_notes": "<=1 sentence"}. '
    "Definitions: CALM = steady low-volatility drift; NEUTRAL = mixed / unclear; "
    "VOLATILE = large whippy moves (rich for big-move capture but risky); "
    "AVOID = no edge, major event risk, or thin liquidity. Be conservative."
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


class LLMContextProvider(ContextProvider):
    """LLM-powered morning briefing provider (OpenAI-compatible endpoints).

    No-op unless ``BRAIN_LLM_ENABLED=true``.  Reads config from the environment
    at construction so a deploy can flip it on without code changes.
    """

    name = "llm_context"

    def __init__(self) -> None:
        self._enabled = _env_bool("BRAIN_LLM_ENABLED", False)
        self._api_key = os.getenv("BRAIN_LLM_API_KEY", "").strip()
        self._base_url = os.getenv("BRAIN_LLM_BASE_URL", "").strip() or _DEFAULT_BASE_URL
        self._model = os.getenv("BRAIN_LLM_MODEL", "").strip() or _DEFAULT_MODEL
        self._timeout_s = _env_float("BRAIN_LLM_TIMEOUT_S", 20.0)
        self._max_tokens = _env_int("BRAIN_LLM_MAX_TOKENS", 512)
        self._temperature = _env_float("BRAIN_LLM_TEMPERATURE", 0.2)
        self._json_mode = _env_bool("BRAIN_LLM_JSON_MODE", True)

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
        except Exception as exc:  # never raise — contract
            logger.warning("llm_context failed date=%s error=%s", trade_date, exc)
            return {}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_llm(self, trade_date: date) -> dict[str, Any]:
        """Query the configured endpoint and map the response to ``llm.*`` keys."""
        features = self._market_context(trade_date)
        messages = self._build_messages(trade_date, features)
        content = chat_completion(
            base_url=self._base_url,
            api_key=self._api_key,
            model=self._model,
            messages=messages,
            timeout_s=self._timeout_s,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            json_mode=self._json_mode,
        )
        obj = extract_json_object(content)
        result = self._normalise(obj)
        if not result:
            raise LLMClientError(f"no usable fields in model response: {obj!r}")
        result[f"{_CONTEXT_PREFIX}model"] = self._model
        logger.info(
            "llm_context date=%s model=%s assessment=%s",
            trade_date,
            self._model,
            result.get(f"{_CONTEXT_PREFIX}day_assessment", "?"),
        )
        return result

    @staticmethod
    def _market_context(trade_date: date) -> dict[str, Any]:
        """Pull the day's quantitative regime features to ground the prompt.

        Reuses :class:`DailyFeaturesProvider` (same file + path resolution).
        Returns the ``daily.*`` features with the prefix stripped, or ``{}`` if
        the feature file is absent — the prompt then notes "no context".
        """
        try:
            override = os.getenv("BRAIN_LLM_FEATURES_PATH", "").strip()
            provider = DailyFeaturesProvider(path=Path(override) if override else None)
            feats = provider.provide(trade_date)
        except Exception:  # feature file is best-effort context, not required
            return {}
        return {
            key[len(_DAILY_PREFIX):]: val
            for key, val in feats.items()
            if key.startswith(_DAILY_PREFIX) and not key.endswith("day_score_hint")
        }

    @staticmethod
    def _build_messages(
        trade_date: date, features: dict[str, Any]
    ) -> list[dict[str, str]]:
        import json as _json

        context = _json.dumps(features, sort_keys=True) if features else "none available"
        user = (
            f"Date: {trade_date.isoformat()} (Asia/Kolkata).\n"
            f"Quantitative context (rolling daily features): {context}.\n"
            "Classify today's trading conditions."
        )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _normalise(obj: dict[str, Any]) -> dict[str, Any]:
        """Map a model JSON object to validated ``llm.*`` context keys."""
        out: dict[str, Any] = {}

        assessment = str(obj.get("day_assessment", "")).strip().upper()
        if assessment in _VALID_ASSESSMENTS:
            out[f"{_CONTEXT_PREFIX}day_assessment"] = assessment

        confidence = obj.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            out[f"{_CONTEXT_PREFIX}confidence"] = max(0.0, min(1.0, float(confidence)))

        for src in ("reasoning", "risk_notes"):
            val = obj.get(src)
            if isinstance(val, str) and val.strip():
                out[f"{_CONTEXT_PREFIX}{src}"] = val.strip()[:500]

        return out


__all__ = ["LLMContextProvider"]
