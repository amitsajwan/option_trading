from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


class DashboardStrategyEvaluationRouter:
    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        get_strategy_eval_service: Callable[[], Any],
        normalize_timestamp_fields: Callable[[Any], Any],
    ) -> None:
        self._templates = templates
        self._get_strategy_eval_service = get_strategy_eval_service
        self._normalize_timestamp_fields = normalize_timestamp_fields

        router = APIRouter(tags=["strategy-evaluation"])
        router.add_api_route("/strategy/evaluation", self.strategy_evaluation_page, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/api/strategy/evaluation/summary", self.get_strategy_evaluation_summary, methods=["GET"])
        router.add_api_route("/api/strategy/evaluation/equity", self.get_strategy_evaluation_equity, methods=["GET"])
        router.add_api_route("/api/strategy/evaluation/days", self.get_strategy_evaluation_days, methods=["GET"])
        router.add_api_route("/api/strategy/evaluation/trades", self.get_strategy_evaluation_trades, methods=["GET"])
        router.add_api_route("/api/strategy/evaluation/runs", self.create_strategy_evaluation_run, methods=["POST"])
        router.add_api_route("/api/strategy/evaluation/runs/latest", self.get_latest_strategy_evaluation_run, methods=["GET"])
        router.add_api_route("/api/strategy/evaluation/runs/{run_id}", self.get_strategy_evaluation_run, methods=["GET"])
        self.router = router

    async def strategy_evaluation_page(self, request: Request) -> HTMLResponse:
        self._require_strategy_eval_service()
        return self._templates.TemplateResponse(
            "strategy_evaluation.html",
            {
                "request": request,
            },
        )

    def _require_strategy_eval_service(self) -> Any:
        service = self._get_strategy_eval_service()
        if service is None:
            raise HTTPException(status_code=500, detail="strategy evaluation service unavailable")
        return service

    def _parse_filters(
        self,
        service: Any,
        *,
        dataset: str,
        date_from: Optional[str],
        date_to: Optional[str],
        strategy: Optional[str],
        regime: Optional[str],
        run_id: Optional[str],
        initial_capital: float,
        cost_bps: float,
        page: int,
        page_size: int,
        sort_by: str,
        sort_dir: str,
    ) -> Dict[str, Any]:
        return service.parse_filters(
            dataset=dataset,
            date_from=str(date_from or ""),
            date_to=str(date_to or ""),
            strategy_raw=strategy,
            regime_raw=regime,
            run_id_raw=run_id,
            initial_capital=float(initial_capital),
            cost_bps=float(cost_bps),
            page=int(page),
            page_size=int(page_size),
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

    async def get_strategy_evaluation_summary(
        self,
        dataset: str = "historical",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        strategy: Optional[str] = None,
        regime: Optional[str] = None,
        run_id: Optional[str] = None,
        initial_capital: float = 1000.0,
        cost_bps: float = 0.0,
    ) -> Any:
        service = self._require_strategy_eval_service()
        try:
            filt = self._parse_filters(
                service,
                dataset=dataset,
                date_from=date_from,
                date_to=date_to,
                strategy=strategy,
                regime=regime,
                run_id=run_id,
                initial_capital=initial_capital,
                cost_bps=cost_bps,
                page=1,
                page_size=50,
                sort_by="exit_time",
                sort_dir="desc",
            )
            payload = service.compute_summary(
                dataset=filt["dataset"],
                date_from=filt["date_from"],
                date_to=filt["date_to"],
                strategies=filt["strategies"],
                regimes=filt["regimes"],
                initial_capital=filt["initial_capital"],
                cost_bps=filt["cost_bps"],
                run_id=filt["run_id"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return self._normalize_timestamp_fields(payload)

    async def get_strategy_evaluation_equity(
        self,
        dataset: str = "historical",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        strategy: Optional[str] = None,
        regime: Optional[str] = None,
        run_id: Optional[str] = None,
        initial_capital: float = 1000.0,
        cost_bps: float = 0.0,
    ) -> Any:
        service = self._require_strategy_eval_service()
        try:
            filt = self._parse_filters(
                service,
                dataset=dataset,
                date_from=date_from,
                date_to=date_to,
                strategy=strategy,
                regime=regime,
                run_id=run_id,
                initial_capital=initial_capital,
                cost_bps=cost_bps,
                page=1,
                page_size=50,
                sort_by="exit_time",
                sort_dir="desc",
            )
            payload = service.compute_equity(
                dataset=filt["dataset"],
                date_from=filt["date_from"],
                date_to=filt["date_to"],
                strategies=filt["strategies"],
                regimes=filt["regimes"],
                initial_capital=filt["initial_capital"],
                cost_bps=filt["cost_bps"],
                run_id=filt["run_id"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return self._normalize_timestamp_fields(payload)

    async def get_strategy_evaluation_days(
        self,
        dataset: str = "historical",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        strategy: Optional[str] = None,
        regime: Optional[str] = None,
        run_id: Optional[str] = None,
        initial_capital: float = 1000.0,
        cost_bps: float = 0.0,
        page: int = 1,
        page_size: int = 50,
    ) -> Any:
        service = self._require_strategy_eval_service()
        try:
            filt = self._parse_filters(
                service,
                dataset=dataset,
                date_from=date_from,
                date_to=date_to,
                strategy=strategy,
                regime=regime,
                run_id=run_id,
                initial_capital=initial_capital,
                cost_bps=cost_bps,
                page=page,
                page_size=page_size,
                sort_by="exit_time",
                sort_dir="desc",
            )
            payload = service.compute_days(
                dataset=filt["dataset"],
                date_from=filt["date_from"],
                date_to=filt["date_to"],
                strategies=filt["strategies"],
                regimes=filt["regimes"],
                initial_capital=filt["initial_capital"],
                cost_bps=filt["cost_bps"],
                page=filt["page"],
                page_size=filt["page_size"],
                run_id=filt["run_id"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return self._normalize_timestamp_fields(payload)

    async def get_strategy_evaluation_trades(
        self,
        dataset: str = "historical",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        strategy: Optional[str] = None,
        regime: Optional[str] = None,
        run_id: Optional[str] = None,
        initial_capital: float = 1000.0,
        cost_bps: float = 0.0,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "exit_time",
        sort_dir: str = "desc",
    ) -> Any:
        service = self._require_strategy_eval_service()
        try:
            filt = self._parse_filters(
                service,
                dataset=dataset,
                date_from=date_from,
                date_to=date_to,
                strategy=strategy,
                regime=regime,
                run_id=run_id,
                initial_capital=initial_capital,
                cost_bps=cost_bps,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )
            payload = service.compute_trades(
                dataset=filt["dataset"],
                date_from=filt["date_from"],
                date_to=filt["date_to"],
                strategies=filt["strategies"],
                regimes=filt["regimes"],
                initial_capital=filt["initial_capital"],
                cost_bps=filt["cost_bps"],
                page=filt["page"],
                page_size=filt["page_size"],
                sort_by=filt["sort_by"],
                sort_dir=filt["sort_dir"],
                run_id=filt["run_id"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return self._normalize_timestamp_fields(payload)

    async def create_strategy_evaluation_run(self, request: Request) -> Any:
        service = self._require_strategy_eval_service()
        payload: Dict[str, Any] = {}
        try:
            body = await request.json()
            if isinstance(body, dict):
                payload = body
        except Exception:
            payload = {}

        dataset = str(payload.get("dataset") or "historical").strip().lower()
        date_from = str(payload.get("date_from") or "").strip()
        date_to = str(payload.get("date_to") or "").strip()
        speed = float(payload.get("speed") or 0.0)
        base_path = str(payload.get("base_path") or "").strip() or None
        risk_config = {
            "stop_loss_pct": payload.get("stop_loss_pct"),
            "target_pct": payload.get("target_pct"),
            "trailing_enabled": payload.get("trailing_enabled"),
            "trailing_activation_pct": payload.get("trailing_activation_pct"),
            "trailing_offset_pct": payload.get("trailing_offset_pct"),
            "trailing_lock_breakeven": payload.get("trailing_lock_breakeven"),
        }
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        try:
            result = service.queue_replay_run(
                dataset=dataset,
                date_from=date_from,
                date_to=date_to,
                speed=float(speed),
                base_path=base_path,
                risk_config=risk_config,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to queue run: {exc}")
        return self._normalize_timestamp_fields(result)

    async def get_latest_strategy_evaluation_run(self, dataset: str = "historical", status: str = "completed") -> Any:
        service = self._require_strategy_eval_service()
        item = service.get_latest_run(dataset=str(dataset or "historical"), status=str(status or "completed"))
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=404,
                detail=f"no run found for dataset='{dataset}' status='{status}'",
            )
        return self._normalize_timestamp_fields(item)

    async def get_strategy_evaluation_run(self, run_id: str) -> Any:
        service = self._require_strategy_eval_service()
        item = service.get_run(run_id=str(run_id))
        if not isinstance(item, dict):
            raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
        return self._normalize_timestamp_fields(item)
