"""GEMINI GROUNDING CACHE — the live "knowledge layer" for direction.

Gemini browses (news / RBI / macro / F&O-expiry / FII-DII) via the native
``google_search`` tool; the result is a short text string that feeds the Groq
direction advisor as `web_context`. This is the ONE source of information our
structural senses lack — and the only plausible direction edge left after the
2026-06-10 structural-only test (project_llm_direction_test_2026-06-10).

This module adds a process-local TTL cache on top of ``oversight.gemini_web``:
  - Gemini's free grounding tier 429s easily, so we fetch at most once per
    ``ttl_seconds`` (default 30 min) and serve the cached string in between.
  - Never raises: any failure → "" (the advisor then runs on structural facts only).
  - A stale-but-non-empty cache is preferred over "" when a refresh fails, so a
    transient 429 does not blank out grounding the engine already had.

Enable with GROUNDING_ENABLED=1 + GEMINI_WEB_API_KEY (see ops/gcp/llm_providers.env.example).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .oversight.gemini_web import fetch_web_context

logger = logging.getLogger(__name__)

_DEFAULT_TTL_S = 1800  # 30 minutes


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class _CacheEntry:
    text: str
    fetched_at: float


class GeminiGrounding:
    """TTL-cached web-grounding provider. Thread-safe; degrades to '' silently."""

    def __init__(self, *, api_key: str = "", model: str = "", ttl_seconds: int = 0,
                 timeout_s: float = 25.0, enabled: Optional[bool] = None) -> None:
        self._api_key = (api_key or os.getenv("GEMINI_WEB_API_KEY", "")
                         or os.getenv("BRAIN_LLM_API_KEY", "")).strip()
        self._model = (model or os.getenv("GEMINI_WEB_MODEL", "")).strip() or "gemini-2.5-flash"
        self._ttl = ttl_seconds or int(os.getenv("GROUNDING_TTL_SECONDS", "") or _DEFAULT_TTL_S)
        self._timeout_s = timeout_s
        self._enabled = _env_bool("GROUNDING_ENABLED", False) if enabled is None else enabled
        self._lock = threading.Lock()
        self._cache: _CacheEntry | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._api_key)

    def get(self, *, force: bool = False) -> str:
        """Return current grounding text. Fetches if cache is empty/expired; else cached.

        On a failed refresh, returns the last good (stale) text rather than "".
        """
        if not self.enabled:
            return ""
        now = time.time()
        with self._lock:
            fresh = self._cache is not None and (now - self._cache.fetched_at) < self._ttl
            if self._cache is not None and fresh and not force:
                return self._cache.text
            # need a (re)fetch
            text = fetch_web_context(api_key=self._api_key, model=self._model,
                                     timeout_s=self._timeout_s)
            if text:
                self._cache = _CacheEntry(text=text, fetched_at=now)
                logger.info("grounding refreshed (%d chars)", len(text))
                return text
            # refresh failed — keep serving the last good value if we have one
            if self._cache is not None:
                logger.warning("grounding refresh failed; serving stale (%.0fs old)",
                               now - self._cache.fetched_at)
                return self._cache.text
            return ""

    def age_seconds(self) -> Optional[float]:
        with self._lock:
            return None if self._cache is None else time.time() - self._cache.fetched_at


__all__ = ["GeminiGrounding"]
