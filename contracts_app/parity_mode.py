from __future__ import annotations

from enum import Enum


class ParityMode(str, Enum):
    """Execution context for a run.

    Determines whether decisions were made on live market data or replayed
    historical data, and whether the full pipeline or only snapshot ingestion
    was replayed.

    Maps to the existing ``source_mode`` / ``Kind`` strings via
    :func:`infer_parity_mode`.
    """

    LIVE_FULL = "live_full"
    REPLAY_SNAPSHOT_ONLY = "replay_snapshot_only"
    REPLAY_FULL = "replay_full"


def infer_parity_mode(source_mode: str) -> ParityMode:
    """Map existing source_mode / Kind strings to :class:`ParityMode`.

    Canonical mapping::

        'live'                  → LIVE_FULL
        'oos'                   → REPLAY_SNAPSHOT_ONLY
        'sim'                   → REPLAY_FULL
        ParityMode value string → the corresponding ParityMode member
        anything else           → LIVE_FULL  (safe fallback)
    """
    s = str(source_mode or "").strip().lower()
    _map: dict[str, ParityMode] = {
        "live": ParityMode.LIVE_FULL,
        "oos": ParityMode.REPLAY_SNAPSHOT_ONLY,
        "sim": ParityMode.REPLAY_FULL,
        "replay": ParityMode.REPLAY_FULL,
        "live_full": ParityMode.LIVE_FULL,
        "replay_snapshot_only": ParityMode.REPLAY_SNAPSHOT_ONLY,
        "replay_full": ParityMode.REPLAY_FULL,
    }
    return _map.get(s, ParityMode.LIVE_FULL)


__all__ = ["ParityMode", "infer_parity_mode"]
