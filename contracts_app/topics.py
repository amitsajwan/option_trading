from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Transport classification (arch/streams-loose-coupling)
#
# DURABLE (Redis Streams, stream: prefix):
#   stream:snapshots:live          — live market snapshots (snapshot_app → strategy_app, persistence_app)
#   stream:snapshots:historical    — OOS/replay snapshots
#   stream:eval:commands           — evaluation run commands (dashboard → orchestrator)
#   stream:eval:progress:{run_id}  — per-run progress events (orchestrator → dashboard history)
#
# DISPLAY_ONLY (Redis Pub/Sub, intentionally ephemeral — do NOT migrate to Streams):
#   market:ohlc:{symbol}:{tf}      — real-time OHLC bars for charting
#   market:tick:{symbol}:*         — raw tick feed for live price display
#   indicators:{symbol}:*          — derived indicator values for UI
#   auth:status                    — auth token push
#   strategy:eval:run:{id}         — live WS progress bridge (shadow; stream:eval:progress is durable)
#   strategy:eval:global           — global run lifecycle events (WS bridge)
#
# Shadow flags (migration controls, default true during Sprint 1–3):
#   SNAPSHOT_PUBSUB_SHADOW          — snapshot_app also PUBLISHes while migrating consumers
#   EVAL_COMMANDS_PUBSUB_SHADOW     — dashboard also PUBLISHes eval command for backward compat
# ---------------------------------------------------------------------------


def snapshot_topic() -> str:
    return (
        str(os.getenv("SNAPSHOT_V1_TOPIC") or os.getenv("LIVE_TOPIC") or "market:snapshot:v1").strip()
        or "market:snapshot:v1"
    )


def historical_snapshot_topic() -> str:
    return (
        str(os.getenv("HISTORICAL_TOPIC") or f"{snapshot_topic()}:historical").strip()
        or f"{snapshot_topic()}:historical"
    )


def strategy_vote_topic() -> str:
    return (
        str(os.getenv("STRATEGY_VOTE_TOPIC") or "market:strategy:votes:v1").strip()
        or "market:strategy:votes:v1"
    )


def trade_signal_topic() -> str:
    return (
        str(os.getenv("TRADE_SIGNAL_TOPIC") or "market:strategy:signals:v1").strip()
        or "market:strategy:signals:v1"
    )


def strategy_position_topic() -> str:
    return (
        str(os.getenv("STRATEGY_POSITION_TOPIC") or "market:strategy:positions:v1").strip()
        or "market:strategy:positions:v1"
    )


def strategy_decision_trace_topic() -> str:
    return (
        str(os.getenv("STRATEGY_DECISION_TRACE_TOPIC") or "market:strategy:decision_trace:v1").strip()
        or "market:strategy:decision_trace:v1"
    )


# ── Phase 2 stream-native decision pipeline topics ──
# DISPLAY_ONLY in live/oos mode (pub/sub). In sim mode use Namespace.stream_for().
# D1 will unify transport so live/oos also routes through streams.


def regime_decisions_topic() -> str:
    return (
        str(os.getenv("REGIME_DECISIONS_TOPIC") or "market:strategy:regime_decisions:v1").strip()
        or "market:strategy:regime_decisions:v1"
    )


def entry_decisions_topic() -> str:
    return (
        str(os.getenv("ENTRY_DECISIONS_TOPIC") or "market:strategy:entry_decisions:v1").strip()
        or "market:strategy:entry_decisions:v1"
    )


def depth_decisions_topic() -> str:
    return (
        str(os.getenv("DEPTH_DECISIONS_TOPIC") or "market:strategy:depth_decisions:v1").strip()
        or "market:strategy:depth_decisions:v1"
    )


def direction_decisions_topic() -> str:
    return (
        str(os.getenv("DIRECTION_DECISIONS_TOPIC") or "market:strategy:direction_decisions:v1").strip()
        or "market:strategy:direction_decisions:v1"
    )


def strike_decisions_topic() -> str:
    return (
        str(os.getenv("STRIKE_DECISIONS_TOPIC") or "market:strategy:strike_decisions:v1").strip()
        or "market:strategy:strike_decisions:v1"
    )


def risk_decisions_topic() -> str:
    return (
        str(os.getenv("RISK_DECISIONS_TOPIC") or "market:strategy:risk_decisions:v1").strip()
        or "market:strategy:risk_decisions:v1"
    )


def execution_events_topic() -> str:
    return (
        str(os.getenv("EXECUTION_EVENTS_TOPIC") or "market:strategy:execution_events:v1").strip()
        or "market:strategy:execution_events:v1"
    )
