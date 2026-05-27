from __future__ import annotations

from typing import Any

try:
    from .._namespace import BASE_SNAPSHOTS
    from .live_strategy_repository import LiveStrategyRepository
except ImportError:
    from market_data_dashboard._namespace import BASE_SNAPSHOTS  # type: ignore
    from market_data_dashboard.services.live_strategy_repository import LiveStrategyRepository  # type: ignore


class HistoricalReplayRepository(LiveStrategyRepository):
    def __init__(self, evaluation_service: Any) -> None:
        super().__init__(
            evaluation_service,
            dataset="historical",
            snapshot_collection_env="MONGO_COLL_SNAPSHOTS_HISTORICAL",
            default_snapshot_collection=f"{BASE_SNAPSHOTS}_historical",
        )


__all__ = ["HistoricalReplayRepository"]
