from __future__ import annotations

import os


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
