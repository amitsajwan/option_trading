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

from .._namespace import normalize_kind
from ..state.strategy_current_state import (
    read_blocker_funnel,
    read_decision_timeline,
    read_observability_summary,
    read_session_heatmap,
    read_strategy_current_state,
)
from ..state.brain_state import read_brain_state


class StrategyCurrentRouter:
    """Single endpoint exposing JSONL-backed current-strategy-session state."""

    def __init__(self) -> None:
        router = APIRouter(tags=["strategy-current"])
        router.add_api_route(
            "/api/strategy/current/state",
            self.get_state,
            methods=["GET"],
        )
        router.add_api_route(
            "/api/strategy/blocker-funnel",
            self.get_blocker_funnel,
            methods=["GET"],
        )
        router.add_api_route(
            "/api/strategy/decisions",
            self.get_decisions,
            methods=["GET"],
        )
        # One-stop observability summary — deployed model + today's gate
        # counts + today's P&L + last decision. Designed for cron / alerting
        # polls and the dashboard top-banner. See docs/OBSERVABILITY_GUIDE.md.
        router.add_api_route(
            "/api/strategy/observability/summary",
            self.get_observability_summary,
            methods=["GET"],
        )
        router.add_api_route(
            "/api/strategy/brain/status",
            self.get_brain_status,
            methods=["GET"],
        )
        router.add_api_route(
            "/api/strategy/session-heatmap",
            self.get_session_heatmap,
            methods=["GET"],
        )
        self.router = router

    @staticmethod
    def _mode_from_kind_or_mode(kind: str | None, mode: str) -> str:
        if str(kind or "").strip():
            resolved = normalize_kind(kind, default="live")
            return "live" if resolved == "live" else "replay"
        return mode

    async def get_state(
        self,
        mode: str = Query("live", description="live | replay"),
        kind: str | None = Query(None, description="optional namespace: live|oos|sim"),
        latest_n: int = Query(50, ge=0, le=500, description="how many recent position events to include"),
    ) -> dict:
        mode = self._mode_from_kind_or_mode(kind, mode)
        if mode.strip().lower() not in {"live", "replay", "historical"}:
            raise HTTPException(status_code=400, detail="mode must be 'live' or 'replay'")
        try:
            return read_strategy_current_state(mode=mode, latest_n=latest_n)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to read JSONL state: {exc}")

    async def get_blocker_funnel(
        self,
        mode: str = Query("live", description="live | replay"),
        kind: str | None = Query(None, description="optional namespace: live|oos|sim"),
        date: str = Query(..., description="YYYY-MM-DD"),
    ) -> dict:
        mode = self._mode_from_kind_or_mode(kind, mode)
        if mode.strip().lower() not in {"live", "replay", "historical"}:
            raise HTTPException(status_code=400, detail="mode must be 'live' or 'replay'")
        try:
            return read_blocker_funnel(mode=mode, date=date)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to read decision_traces: {exc}")

    async def get_decisions(
        self,
        mode: str = Query("live", description="live | replay"),
        kind: str | None = Query(None, description="optional namespace: live|oos|sim"),
        date: str = Query(..., description="YYYY-MM-DD"),
        limit: int = Query(500, ge=0, le=2000),
        offset: int = Query(0, ge=0),
        outcome: str = Query("", description="empty | blocked | hold | entry_taken | exit_taken | manage_only"),
        collapse: bool = Query(False, description="merge consecutive rows with bit-identical (outcome,gate,reason,entry_prob)"),
    ) -> dict:
        mode = self._mode_from_kind_or_mode(kind, mode)
        if mode.strip().lower() not in {"live", "replay", "historical"}:
            raise HTTPException(status_code=400, detail="mode must be 'live' or 'replay'")
        try:
            return read_decision_timeline(
                mode=mode, date=date, limit=limit, offset=offset,
                outcome=(outcome or None), collapse=collapse,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to read decision_traces: {exc}")

    async def get_observability_summary(
        self,
        mode: str = Query("live", description="live | replay"),
    ) -> dict:
        if mode.strip().lower() not in {"live", "replay", "historical"}:
            raise HTTPException(status_code=400, detail="mode must be 'live' or 'replay'")
        try:
            return read_observability_summary(mode=mode)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to read observability state: {exc}")

    async def get_session_heatmap(
        self,
        mode: str = Query("live", description="live | replay"),
        date: str = Query(..., description="YYYY-MM-DD"),
    ) -> dict:
        """Compact per-minute session heatmap data for a given date.

        Returns one row per traced minute with outcome, shadow score/direction/basis,
        and entry prob — enough for the UI to paint a color-coded session strip.
        """
        if mode.strip().lower() not in {"live", "replay", "historical"}:
            raise HTTPException(status_code=400, detail="mode must be 'live' or 'replay'")
        try:
            return read_session_heatmap(mode=mode, date=date)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to read session heatmap: {exc}")

    async def get_brain_status(
        self,
        mode: str = Query("live", description="live | replay"),
    ) -> dict:
        """Return the TradingBrain morning context for the current/last session.

        Reads brain_state.json written by DeterministicRuleEngine.on_session_start().
        Returns {trade_date, brain_enabled, day_context: {day_score, confidence,
        regime_rv20, sma20_slope, carry_consecutive_losses, size_multiplier, ...}}.
        Returns {available: false} when no brain_state.json exists (engine not
        started or brain disabled).
        """
        if mode.strip().lower() not in {"live", "replay", "historical"}:
            raise HTTPException(status_code=400, detail="mode must be 'live' or 'replay'")
        try:
            return read_brain_state(mode=mode)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to read brain state: {exc}")
