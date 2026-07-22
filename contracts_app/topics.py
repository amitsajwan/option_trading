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

# ---------------------------------------------------------------------------
# Transport classification (arch/streams-loose-coupling)
#
# DURABLE (Redis Streams, stream: prefix):
#   stream:snapshots:live              — primary instrument (BANKNIFTY) live snapshots
#   stream:snapshots:{instr}:live      — secondary instrument (e.g. NIFTY) live snapshots
#   stream:snapshots:historical        — primary instrument OOS/replay snapshots
#   stream:snapshots:{instr}:historical — secondary instrument OOS/replay snapshots
#   stream:eval:commands           — evaluation run commands (dashboard → orchestrator)
#   stream:eval:progress:{run_id}  — per-run progress events (orchestrator → dashboard history)
#
#   Always derive these via stream_name_for_topic(topic) below, from the SAME
#   instrument-scoped topic string snapshot_topic()/historical_snapshot_topic()
#   already produce -- never hardcode "stream:snapshots:live" directly. Found
#   and fixed 2026-07-22: snapshot_app's publisher, strategy_app's consumer,
#   and persistence_app's consumer each independently hardcoded that one
#   string regardless of instrument, so a NIFTY container's live stream would
#   have silently collided with BANKNIFTY's the moment both ran on streams
#   transport at once -- caught only because NIFTY was deployed first, alone,
#   as a rehearsal before BankNifty (which carries real money).
#
# DISPLAY_ONLY (Redis Pub/Sub, intentionally ephemeral — do NOT migrate to Streams):
#   market:ohlc:{symbol}:{tf}      — real-time OHLC bars for charting
#   market:tick:{symbol}:*         — raw tick feed for live price display
#   indicators:{symbol}:*          — derived indicator values for UI
#   auth:status                    — auth token push
#   strategy:eval:run:{id}         — live WS progress bridge (shadow; stream:eval:progress is durable)
#   strategy:eval:global           — global run lifecycle events (WS bridge)
#
# Shadow flags (migration controls):
#   SNAPSHOT_PUBSUB_SHADOW          — NOT IMPLEMENTED. snapshot_app/redis_publisher.py's
#                                     RedisEventPublisher.publish() is XADD-only; this flag
#                                     is not read anywhere. A1's original dual-write design
#                                     (ADR-003) was superseded by committing straight to
#                                     streams-only — this line documented that abandoned
#                                     design and was never updated. Setting this env var
#                                     does nothing. There is no pub/sub fallback for snapshot
#                                     delivery; a real rollback means reverting the deploy.
#   EVAL_COMMANDS_PUBSUB_SHADOW     — real and implemented (strategy_eval_orchestrator/main.py,
#                                     market_data_dashboard/services/strategy_evaluation_service.py):
#                                     dashboard also PUBLISHes eval command for backward compat.
# ---------------------------------------------------------------------------


def snapshot_topic() -> str:
    return _resolve_topic(["SNAPSHOT_V1_TOPIC", "LIVE_TOPIC"], "market:snapshot:v1")


def historical_snapshot_topic() -> str:
    explicit = str(os.getenv("HISTORICAL_TOPIC") or "").strip()
    if explicit:
        return explicit
    return f"{snapshot_topic()}:historical"


def stream_name_for_topic(topic: str) -> str:
    """Derive the durable Streams name from a pub/sub-style snapshot topic.

    Producer (snapshot_app's RedisEventPublisher) and consumers (strategy_app's
    RedisSnapshotConsumer, persistence_app's main_snapshot_consumer) must derive
    the SAME stream name from the SAME topic string, or different instruments
    silently share one stream. `topic` is whatever snapshot_topic()/
    historical_snapshot_topic() already produced (instrument-scoped via
    STRATEGY_INSTRUMENT or an explicit SNAPSHOT_V1_TOPIC/LIVE_TOPIC override),
    so no separate instrument lookup is needed here -- just re-shape the string.

        market:snapshot:v1                  -> stream:snapshots:live
        market:nifty:snapshot:v1            -> stream:snapshots:nifty:live
        market:snapshot:v1:historical       -> stream:snapshots:historical
        market:nifty:snapshot:v1:historical -> stream:snapshots:nifty:historical

    Falls back to "stream:snapshots:live" for any topic that doesn't match the
    expected market:[instr:]snapshot:v1[:historical] shape (e.g. sim's
    stream:*:sim:{run_id} names, which are already Streams-native and should
    never reach this function, but a safe default beats a crash).
    """
    parts = [p for p in str(topic or "").strip().split(":") if p]
    is_historical = "historical" in parts
    instr: Optional[str] = None
    if len(parts) >= 3 and parts[0] == "market":
        if parts[1] == "snapshot":
            instr = None
        elif len(parts) >= 4 and parts[2] == "snapshot":
            instr = parts[1]
        else:
            return "stream:snapshots:live"
    else:
        return "stream:snapshots:live"
    suffix = "historical" if is_historical else "live"
    if instr:
        return f"stream:snapshots:{instr}:{suffix}"
    return f"stream:snapshots:{suffix}"


def strategy_vote_topic() -> str:
    return _resolve_topic(["STRATEGY_VOTE_TOPIC"], "market:strategy:votes:v1")


def trade_signal_topic() -> str:
    return _resolve_topic(["TRADE_SIGNAL_TOPIC"], "market:strategy:signals:v1")


def strategy_position_topic() -> str:
    return _resolve_topic(["STRATEGY_POSITION_TOPIC"], "market:strategy:positions:v1")


def strategy_decision_trace_topic() -> str:
    return _resolve_topic(["STRATEGY_DECISION_TRACE_TOPIC"], "market:strategy:decision_trace:v1")


# ── Phase 2 stream-native decision pipeline topics ──
# DISPLAY_ONLY in live/oos mode (pub/sub). In sim mode use Namespace.stream_for().
# D1 will unify transport so live/oos also routes through streams.


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
