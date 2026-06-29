from __future__ import annotations

import os
from typing import Optional

from .sim_namespace import _instrument_slug, normalize_instrument

# ── Instrument-aware live/oos pubsub topics ──────────────────────────────────
# These are the topic names the LIVE pipeline publishes/consumes on (distinct
# from Namespace.stream_for, which sim uses). They gain instrument scoping via a
# single env var, STRATEGY_INSTRUMENT, so one setting scopes a whole container:
#
#   primary (BANKNIFTY): market:snapshot:v1        (byte-identical to legacy)
#   secondary (NIFTY):   market:nifty:snapshot:v1
#
# Precedence: an explicit per-topic env override (e.g. SNAPSHOT_V1_TOPIC) always
# wins and is used verbatim — so a NIFTY container can either set STRATEGY_INSTRUMENT
# once, or pin each topic explicitly.


def scope_topic_for_instrument(default_topic: str, instrument: Optional[str] = None) -> str:
    """Insert ``instrument``'s slug into a default topic.

    Primary instrument -> unchanged. Slug is inserted right after the
    ``market:`` prefix so the family stays grouped (``market:nifty:...``).
    Pass an explicit instrument (e.g. from a dashboard query param) or rely on
    STRATEGY_INSTRUMENT when ``instrument`` is None.
    """
    canon = instrument if instrument is not None else os.getenv("STRATEGY_INSTRUMENT")
    slug = _instrument_slug(normalize_instrument(canon))
    if not slug:
        return default_topic
    prefix = "market:"
    if default_topic.startswith(prefix):
        return f"{prefix}{slug}:{default_topic[len(prefix):]}"
    return f"{slug}:{default_topic}"


def _scope_to_instrument(default_topic: str) -> str:
    """Env-driven scoping (STRATEGY_INSTRUMENT) used by the live topic helpers."""
    return scope_topic_for_instrument(default_topic, None)


def _resolve_topic(env_names: list[str], default_topic: str) -> str:
    """Explicit env override (verbatim) wins; else instrument-scoped default."""
    for name in env_names:
        raw = str(os.getenv(name) or "").strip()
        if raw:
            return raw
    return _scope_to_instrument(default_topic)


def snapshot_topic() -> str:
    return _resolve_topic(["SNAPSHOT_V1_TOPIC", "LIVE_TOPIC"], "market:snapshot:v1")


def historical_snapshot_topic() -> str:
    explicit = str(os.getenv("HISTORICAL_TOPIC") or "").strip()
    if explicit:
        return explicit
    return f"{snapshot_topic()}:historical"


def strategy_vote_topic() -> str:
    return _resolve_topic(["STRATEGY_VOTE_TOPIC"], "market:strategy:votes:v1")


def trade_signal_topic() -> str:
    return _resolve_topic(["TRADE_SIGNAL_TOPIC"], "market:strategy:signals:v1")


def strategy_position_topic() -> str:
    return _resolve_topic(["STRATEGY_POSITION_TOPIC"], "market:strategy:positions:v1")


def strategy_decision_trace_topic() -> str:
    return _resolve_topic(["STRATEGY_DECISION_TRACE_TOPIC"], "market:strategy:decision_trace:v1")


# ── Phase 2 stream-native decision pipeline topics (live/oos pubsub mode) ──
# In sim mode use Namespace.stream_for() instead of these functions.


def regime_decisions_topic() -> str:
    return _resolve_topic(["REGIME_DECISIONS_TOPIC"], "market:strategy:regime_decisions:v1")


def entry_decisions_topic() -> str:
    return _resolve_topic(["ENTRY_DECISIONS_TOPIC"], "market:strategy:entry_decisions:v1")


def depth_decisions_topic() -> str:
    return _resolve_topic(["DEPTH_DECISIONS_TOPIC"], "market:strategy:depth_decisions:v1")


def direction_decisions_topic() -> str:
    return _resolve_topic(["DIRECTION_DECISIONS_TOPIC"], "market:strategy:direction_decisions:v1")


def strike_decisions_topic() -> str:
    return _resolve_topic(["STRIKE_DECISIONS_TOPIC"], "market:strategy:strike_decisions:v1")


def risk_decisions_topic() -> str:
    return _resolve_topic(["RISK_DECISIONS_TOPIC"], "market:strategy:risk_decisions:v1")


def execution_events_topic() -> str:
    return _resolve_topic(["EXECUTION_EVENTS_TOPIC"], "market:strategy:execution_events:v1")
