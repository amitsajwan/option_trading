from __future__ import annotations

from typing import Any

try:
    from .live_strategy_repository import LiveStrategyRepository
except ImportError:
    from live_strategy_repository import LiveStrategyRepository  # type: ignore


class HistoricalReplayRepository(LiveStrategyRepository):
    def __init__(self, evaluation_service: Any) -> None:
        super().__init__(
            evaluation_service,
            dataset="historical",
            snapshot_collection_env="MONGO_COLL_SNAPSHOTS_HISTORICAL",
            default_snapshot_collection="phase1_market_snapshots_historical",
        )


__all__ = ["HistoricalReplayRepository"]
