"""SessionBias — the STATEFUL session sense (morning thesis, refreshed ~30 min).

A human trader forms a morning view from live news + the day's levels and *holds*
it. This is that, as a sense:
  - at the open (first call) and every ~30 min, ask Gemini (search-grounded) for a
    structured brief: day_bias / conviction / grounded / news / key_levels / plan,
    built from OUR real levels (walls, prev-day, ORB, VIX),
  - cache it for the session (keyed by trade_date) and serve it between refreshes,
  - expose .side / .conviction / .grounded so the mind can use it as a *confirmer*
    (e.g. don't fight a high-conviction grounded bias), NEVER as ground truth.

Discipline (carried from project_llm_direction_test_2026-06-10):
  - Soft sense, shadow-first. An ungrounded brief (Gemini couldn't retrieve real
    news) is forced NEUTRAL so hallucinations can't steer the book.
  - Never raises; degrades to a NEUTRAL bias. Off unless GROUNDING_ENABLED + key.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..market.snapshot_accessor import SnapshotAccessor
from .oversight.gemini_web import fetch_session_brief

logger = logging.getLogger(__name__)

_DEFAULT_TTL_S = 1800  # 30 min


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SessionBias:
    day_bias: str = "NEUTRAL"          # BULLISH | BEARISH | NEUTRAL
    conviction: float = 0.0
    grounded: bool = False
    news_summary: str = ""
    key_levels: Dict[str, Any] = field(default_factory=dict)
    plan: str = ""
    risks: str = ""
    as_of: str = ""
    trade_date: str = ""
    fetched_at: float = 0.0

    @property
    def side(self) -> Optional[str]:
        """CE on a grounded bullish bias, PE on grounded bearish, else None (no lean)."""
        if not self.grounded:
            return None
        if self.day_bias == "BULLISH":
            return "CE"
        if self.day_bias == "BEARISH":
            return "PE"
        return None

    def as_sense(self) -> Dict[str, Any]:
        return {
            "session_bias": self.day_bias,
            "session_bias_side": self.side,
            "session_conviction": round(self.conviction, 3),
            "session_grounded": self.grounded,
            "session_news": self.news_summary[:240],
            "session_as_of": self.as_of,
        }


def build_context(snap: SnapshotAccessor) -> Dict[str, Any]:
    """Extract the real levels Gemini should analyse from a snapshot."""
    raw = getattr(snap, "raw_payload", None) or {}
    ca = raw.get("chain_aggregates") or {}
    orr = raw.get("opening_range") or {}
    sl = raw.get("session_levels") or {}
    sc = raw.get("session_context") or {}
    vix = raw.get("vix_context") or {}
    return {
        "date": snap.trade_date,
        "time": sc.get("time"),
        "days_to_expiry": snap.days_to_expiry,
        "spot": snap.fut_close,
        "open": (raw.get("futures_bar") or {}).get("open"),
        "prev_day_high": sl.get("prev_day_high"),
        "prev_day_low": sl.get("prev_day_low"),
        "prev_day_close": sl.get("prev_day_close"),
        "orb_high": orr.get("orh"),
        "orb_low": orr.get("orl"),
        "call_wall": ca.get("ce_oi_top_strike"),
        "put_wall": ca.get("pe_oi_top_strike"),
        "max_pain": ca.get("max_pain"),
        "vix": vix.get("vix_current"),
        "vix_regime": vix.get("vix_regime"),
    }


class SessionBiasStore:
    """Process-local, thread-safe, TTL-refreshed session bias. Call update() on the
    oversight cadence (~every 30 bars), read current() in the hot path."""

    def __init__(self, *, api_key: str = "", model: str = "", ttl_seconds: int = 0,
                 enabled: Optional[bool] = None) -> None:
        self._api_key = (api_key or os.getenv("GEMINI_WEB_API_KEY", "")
                         or os.getenv("BRAIN_LLM_API_KEY", "")).strip()
        self._model = (model or os.getenv("GEMINI_WEB_MODEL", "")).strip() or "gemini-2.5-flash"
        self._ttl = ttl_seconds or int(os.getenv("GROUNDING_TTL_SECONDS", "") or _DEFAULT_TTL_S)
        self._enabled = _env_bool("GROUNDING_ENABLED", False) if enabled is None else enabled
        self._lock = threading.Lock()
        self._bias: Optional[SessionBias] = None

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._api_key)

    def update(self, snap: SnapshotAccessor, *, force: bool = False) -> Optional[SessionBias]:
        """Refresh if new day / stale / forced; otherwise keep the held thesis."""
        if not self.enabled:
            return self._bias
        day = snap.trade_date
        now = time.time()
        with self._lock:
            cur = self._bias
            fresh = (cur is not None and cur.trade_date == day
                     and (now - cur.fetched_at) < self._ttl)
            if fresh and not force:
                return cur
            try:
                brief = fetch_session_brief(build_context(snap), api_key=self._api_key, model=self._model)
            except Exception:
                logger.debug("session_bias: fetch failed", exc_info=True)
                return cur  # keep stale rather than blank
            self._bias = SessionBias(
                day_bias=brief.get("day_bias", "NEUTRAL"),
                conviction=float(brief.get("conviction") or 0.0),
                grounded=bool(brief.get("grounded")),
                news_summary=str(brief.get("news_summary") or ""),
                key_levels=dict(brief.get("key_levels") or {}),
                plan=str(brief.get("plan") or ""),
                risks=str(brief.get("risks") or ""),
                as_of=str(brief.get("as_of") or ""),
                trade_date=day,
                fetched_at=now,
            )
            logger.info("session_bias refreshed: %s conv=%.2f grounded=%s",
                        self._bias.day_bias, self._bias.conviction, self._bias.grounded)
            return self._bias

    def current(self) -> Optional[SessionBias]:
        with self._lock:
            return self._bias


__all__ = ["SessionBias", "SessionBiasStore", "build_context"]
