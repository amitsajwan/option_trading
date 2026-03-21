from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


class DashboardResearchRouter:
    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        list_recovery_scenarios_fn: Callable[[], Any],
        evaluate_recovery_scenario_fn: Callable[..., Any],
        research_eval_available: Callable[[], bool],
    ) -> None:
        self._templates = templates
        self._list_recovery_scenarios_fn = list_recovery_scenarios_fn
        self._evaluate_recovery_scenario_fn = evaluate_recovery_scenario_fn
        self._research_eval_available = research_eval_available

        router = APIRouter(tags=["research"])
        router.add_api_route("/trading/research", self.trading_research_page, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/api/trading/research/scenarios", self.get_trading_research_scenarios, methods=["GET"])
        router.add_api_route("/api/trading/research/evaluation", self.get_trading_research_evaluation, methods=["GET"])
        self.router = router

    def _require_research_eval_service(self) -> None:
        if not bool(self._research_eval_available()):
            raise HTTPException(status_code=500, detail="research evaluation service unavailable")

    async def trading_research_page(self, request: Request) -> HTMLResponse:
        self._require_research_eval_service()
        return self._templates.TemplateResponse(
            "trading_research.html",
            {
                "request": request,
            },
        )

    async def get_trading_research_scenarios(self) -> Any:
        self._require_research_eval_service()
        return self._list_recovery_scenarios_fn()

    async def get_trading_research_evaluation(
        self,
        scenario_key: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        recipe_id: Optional[str] = None,
        threshold: Optional[float] = None,
    ) -> Any:
        self._require_research_eval_service()
        try:
            return self._evaluate_recovery_scenario_fn(
                scenario_key=scenario_key,
                date_from=date_from,
                date_to=date_to,
                recipe_id=recipe_id,
                threshold=threshold,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
