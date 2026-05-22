from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


class DashboardModelCatalogRouter:
    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        build_trading_model_catalog: Callable[[], list[dict[str, Any]]],
        legacy_trading_runtime_status: Callable[[], dict[str, Any]],
        normalize_trading_instance: Callable[[Any], str],
        resolve_trading_model_catalog_entry: Callable[[Optional[str]], Any],
        resolve_repo_path: Callable[[Optional[str], Optional[Path]], Any],
        build_model_eval_snapshot: Callable[[Any, Any, Any], dict[str, Any]],
        build_feature_intelligence_snapshot: Callable[[dict[str, Any], Optional[str], Optional[str]], dict[str, Any]],
        default_model_eval_summary_path: Path,
        default_model_training_report_path: Path,
        default_model_policy_report_path: Path,
    ) -> None:
        self._templates = templates
        self._build_trading_model_catalog = build_trading_model_catalog
        self._legacy_trading_runtime_status = legacy_trading_runtime_status
        self._normalize_trading_instance = normalize_trading_instance
        self._resolve_trading_model_catalog_entry = resolve_trading_model_catalog_entry
        self._resolve_repo_path = resolve_repo_path
        self._build_model_eval_snapshot = build_model_eval_snapshot
        self._build_feature_intelligence_snapshot = build_feature_intelligence_snapshot
        self._default_model_eval_summary_path = default_model_eval_summary_path
        self._default_model_training_report_path = default_model_training_report_path
        self._default_model_policy_report_path = default_model_policy_report_path

        router = APIRouter(tags=["model-catalog"])
        router.add_api_route("/trading/models", self.trading_models_page, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/api/trading/models", self.get_trading_models, methods=["GET"])
        router.add_api_route("/trading/model/{model_key}", self.trading_terminal_model, methods=["GET"])
        router.add_api_route("/api/trading/model-evaluation", self.get_trading_model_evaluation, methods=["GET"])
        router.add_api_route("/api/trading/feature-intelligence", self.get_trading_feature_intelligence, methods=["GET"])
        self.router = router

    async def trading_models_page(self, request: Request) -> RedirectResponse:
        return RedirectResponse(url="/app?tab=models", status_code=302)

    async def get_trading_models(self) -> dict[str, Any]:
        models = self._build_trading_model_catalog()
        return {
            "status": "ok",
            "count": len(models),
            "ready_count": sum(1 for m in models if m.get("ready_to_run")),
            "research_count": sum(1 for m in models if str(m.get("catalog_kind") or "") == "recovery"),
            "legacy_trading_runtime": self._legacy_trading_runtime_status(),
            "models": models,
        }

    async def trading_terminal_model(self, model_key: str) -> RedirectResponse:
        safe_key = self._normalize_trading_instance(model_key)
        for entry in self._build_trading_model_catalog():
            if str(entry.get("instance_key") or "").strip().lower() == safe_key.lower():
                prefill_url = str(entry.get("prefill_url") or "").strip()
                if prefill_url:
                    return RedirectResponse(url=prefill_url, status_code=307)
                break
        return RedirectResponse(url=f"/trading?model={safe_key}", status_code=307)

    async def get_trading_model_evaluation(
        self,
        summary_path: Optional[str] = None,
        training_report_path: Optional[str] = None,
        policy_report_path: Optional[str] = None,
    ) -> dict[str, Any]:
        summary_file = self._resolve_repo_path(summary_path, self._default_model_eval_summary_path)
        training_file = self._resolve_repo_path(training_report_path, self._default_model_training_report_path)
        policy_file = self._resolve_repo_path(policy_report_path, self._default_model_policy_report_path)
        snapshot = self._build_model_eval_snapshot(summary_file, training_file, policy_file)
        snapshot["status"] = "ok"
        return snapshot

    async def get_trading_feature_intelligence(
        self,
        model: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict[str, Any]:
        model_entry = self._resolve_trading_model_catalog_entry(model)
        if not isinstance(model_entry, dict):
            return {
                "status": "no_data",
                "message": "No runnable trading model artifacts were discovered.",
                "ranking": {"rows": []},
                "groups": [],
                "scatter": {"points": []},
            }

        snapshot = self._build_feature_intelligence_snapshot(
            model_entry,
            date_from,
            date_to,
        )
        snapshot["status"] = "ok" if snapshot.get("ranking", {}).get("rows") else "no_data"
        return snapshot
