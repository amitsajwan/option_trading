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
