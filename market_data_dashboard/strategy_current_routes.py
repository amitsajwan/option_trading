"""Routes for the currently-running strategy session, JSONL-backed.

Endpoint:
    GET /api/strategy/current/state?mode=live|replay&latest_n=50

Reads directly from the strategy_app run_dir's positions.jsonl + health_marker.
Mongo-independent — surfaces correct data even when the mongo persistence
path is slow or unavailable.

Mode resolution:
    mode=live      → STRATEGY_RUN_DIR_LIVE (default .run/strategy_app)
    mode=replay    → STRATEGY_RUN_DIR_HISTORICAL (default .run/strategy_app_historical)

See ARCHITECTURE.md §9 "Storage and Persistence Contract" for rationale:
this is the "current session / current run" query that lives on JSONL
to avoid mongo lag; cross-day aggregates still use the existing mongo
routes (historical_replay_routes etc).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .strategy_current_state import read_strategy_current_state


class StrategyCurrentRouter:
    """Single endpoint exposing JSONL-backed current-strategy-session state."""

    def __init__(self) -> None:
        router = APIRouter(tags=["strategy-current"])
        router.add_api_route(
            "/api/strategy/current/state",
            self.get_state,
            methods=["GET"],
        )
        self.router = router

    async def get_state(
        self,
        mode: str = Query("live", description="live | replay"),
        latest_n: int = Query(50, ge=0, le=500, description="how many recent position events to include"),
    ) -> dict:
        if mode.strip().lower() not in {"live", "replay", "historical"}:
            raise HTTPException(status_code=400, detail="mode must be 'live' or 'replay'")
        try:
            return read_strategy_current_state(mode=mode, latest_n=latest_n)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to read JSONL state: {exc}")
