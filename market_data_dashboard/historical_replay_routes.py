from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


class DashboardHistoricalReplayRouter:
    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        templates_dir: Path,
        get_historical_replay_service: Callable[[], Any],
        now_iso_ist: Callable[[], str],
    ) -> None:
        self._templates = templates
        self._templates_dir = Path(templates_dir)
        self._get_historical_replay_service = get_historical_replay_service
        self._now_iso_ist = now_iso_ist

        router = APIRouter(tags=["historical-replay"])
        router.add_api_route("/historical/replay", self.historical_replay, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/api/historical/replay/session", self.get_historical_strategy_session, methods=["GET"])
        router.add_api_route("/api/historical/replay/status", self.get_historical_replay_status, methods=["GET"])
        router.add_api_route("/api/health/replay", self.replay_health, methods=["GET"])
        self.router = router

    def _require_service(self) -> Any:
        service = self._get_historical_replay_service()
        if service is None:
            raise HTTPException(status_code=500, detail="historical replay service unavailable")
        return service

    async def historical_replay(self, request: Request) -> HTMLResponse:
        return self._templates.TemplateResponse("historical_replay.html", {"request": request})

    async def get_historical_replay_status(
        self,
        date: Optional[str] = None,
        instrument: Optional[str] = None,
    ) -> Any:
        service = self._require_service()
        try:
            return service.get_replay_status(date=date, instrument=instrument)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    async def get_historical_strategy_session(
        self,
        date: Optional[str] = None,
        instrument: Optional[str] = None,
        run_id: Optional[str] = None,
        limit_votes: int = 25,
        limit_signals: int = 25,
        limit_trades: int = 20,
        initial_capital: Optional[float] = None,
        timeline_limit: int = 25,
        debug_view: int = 0,
    ) -> Any:
        service = self._require_service()
        try:
            return service.get_historical_strategy_session(
                date=date,
                instrument=instrument,
                run_id=run_id,
                limit_votes=limit_votes,
                limit_signals=limit_signals,
                limit_trades=limit_trades,
                initial_capital=initial_capital,
                timeline_limit=timeline_limit,
                debug_view=debug_view,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to build historical replay session: {exc}")

    async def replay_health(self, date: Optional[str] = None, instrument: Optional[str] = None) -> Any:
        service = self._require_service()
        replay_status = service.get_replay_status(date=date, instrument=instrument)
        counts = replay_status.get("collection_counts") if isinstance(replay_status.get("collection_counts"), dict) else {}
        healthy = bool(replay_status.get("data_ready")) and sum(int(v or 0) for v in counts.values()) > 0
        status = "healthy" if healthy else ("degraded" if replay_status.get("status") not in {"unavailable", "failed"} else "unhealthy")
        return {
            "status": status,
            "mode": "historical",
            "timestamp": self._now_iso_ist(),
            "replay": replay_status,
        }


__all__ = ["DashboardHistoricalReplayRouter"]
