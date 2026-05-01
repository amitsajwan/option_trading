from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

import redis
import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from strategy_app.engines.runtime_artifacts import resolve_runtime_artifact_paths


class DashboardOperatorRouter:
    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        templates_dir: Path,
        market_data_api_url: str,
        redis_host: str,
        redis_port: int,
        get_live_strategy_monitor_service: Callable[[], Any],
        get_strategy_eval_service: Callable[[], Any],
        normalize_timestamp_fields: Callable[[Any], Any],
        now_iso_ist: Callable[[], str],
    ) -> None:
        self._templates = templates
        self._templates_dir = Path(templates_dir)
        self._market_data_api_url = str(market_data_api_url or "").rstrip("/")
        self._redis_host = str(redis_host or "localhost")
        self._redis_port = int(redis_port)
        self._get_live_strategy_monitor_service = get_live_strategy_monitor_service
        self._get_strategy_eval_service = get_strategy_eval_service
        self._normalize_timestamp_fields = normalize_timestamp_fields
        self._now_iso_ist = now_iso_ist

        router = APIRouter(tags=["operator"])
        router.add_api_route("/", self.home, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/live/strategy", self.live_strategy, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/api/live/strategy/session", self.get_live_strategy_session, methods=["GET"])
        router.add_api_route("/api/live/strategy/traces", self.get_live_strategy_traces, methods=["GET"])
        router.add_api_route("/api/live/strategy/traces/{trace_id}", self.get_live_strategy_trace_detail, methods=["GET"])
        router.add_api_route("/api/health", self.health, methods=["GET"])
        router.add_api_route("/api/health/live", self.health, methods=["GET"])
        router.add_api_route("/api/market-data/health", self.market_data_health, methods=["GET"])
        router.add_api_route("/api/v1/system/mode", self.get_system_mode, methods=["GET"])
        router.add_api_route("/api/operator/halt", self.post_operator_halt, methods=["POST"])
        router.add_api_route("/api/operator/halt", self.delete_operator_halt, methods=["DELETE"])
        router.add_api_route("/api/operator/halt", self.get_operator_halt, methods=["GET"])
        self.router = router

    async def home(self, request: Request) -> RedirectResponse:
        return RedirectResponse(url="/app", status_code=302)

    async def live_strategy(self, request: Request) -> RedirectResponse:
        return RedirectResponse(url="/app?mode=live", status_code=302)

    def _require_live_strategy_monitor_service(self) -> Any:
        service = self._get_live_strategy_monitor_service()
        if service is None:
            raise HTTPException(status_code=500, detail="live strategy monitor service unavailable")
        return service

    def _request_market_data_json(self, path: str, *, timeout: float) -> dict[str, Any]:
        url = f"{self._market_data_api_url}/{str(path).lstrip('/')}"
        started = time.perf_counter()
        try:
            response = requests.get(url, timeout=timeout)
            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            try:
                payload = response.json()
            except Exception as exc:
                payload = None
                error = f"invalid_json: {exc}"
            else:
                error = None

            normalized_payload = self._normalize_timestamp_fields(payload) if payload is not None else None
            return {
                "ok": bool(response.ok),
                "status_code": int(response.status_code),
                "payload": normalized_payload,
                "error": error,
                "url": url,
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return {
                "ok": False,
                "status_code": 0,
                "payload": None,
                "error": str(exc),
                "url": url,
                "latency_ms": latency_ms,
            }

    def _probe_market_data_health(self) -> dict[str, Any]:
        result = self._request_market_data_json("health", timeout=3.0)
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        upstream_status = str(payload.get("status") or "").strip().lower()
        reachable = int(result.get("status_code") or 0) > 0
        healthy = bool(result["ok"]) and upstream_status == "healthy"
        return {
            "status": "healthy" if healthy else ("degraded" if reachable else "unhealthy"),
            "reachable": reachable,
            "status_code": int(result.get("status_code") or 0),
            "latency_ms": result.get("latency_ms"),
            "url": result.get("url"),
            "timestamp": payload.get("timestamp") if isinstance(payload, dict) else None,
            "error": result.get("error"),
        }

    def _probe_redis(self) -> dict[str, Any]:
        started = time.perf_counter()
        client: Optional[redis.Redis] = None
        try:
            client = redis.Redis(
                host=self._redis_host,
                port=self._redis_port,
                db=0,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            client.ping()
            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return {
                "status": "healthy",
                "reachable": True,
                "host": self._redis_host,
                "port": self._redis_port,
                "latency_ms": latency_ms,
                "error": None,
            }
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return {
                "status": "unhealthy",
                "reachable": False,
                "host": self._redis_host,
                "port": self._redis_port,
                "latency_ms": latency_ms,
                "error": str(exc),
            }
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    async def get_live_strategy_session(
        self,
        date: Optional[str] = None,
        instrument: Optional[str] = None,
        limit_votes: int = 25,
        limit_signals: int = 25,
        limit_trades: int = 20,
        initial_capital: Optional[float] = None,
        timeline_limit: int = 25,
        debug_view: int = 0,
    ) -> Any:
        service = self._require_live_strategy_monitor_service()
        try:
            payload = service.get_live_strategy_session(
                date=date,
                instrument=instrument,
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
            raise HTTPException(status_code=500, detail=f"failed to build live strategy session: {exc}")
        return self._normalize_timestamp_fields(payload)

    async def get_live_strategy_traces(
        self,
        date: Optional[str] = None,
        limit: int = 25,
        outcome: Optional[str] = None,
        engine_mode: Optional[str] = None,
        only_blocked: int = 0,
        snapshot_id: Optional[str] = None,
        position_id: Optional[str] = None,
    ) -> Any:
        service = self._require_live_strategy_monitor_service()
        try:
            date_ist = service.get_session_date_ist(date)
            rows = service.load_recent_trace_digests(
                date_ist,
                limit,
                outcome=outcome,
                engine_mode=engine_mode,
                only_blocked=bool(only_blocked),
                snapshot_id=snapshot_id,
                position_id=position_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to load live strategy traces: {exc}")
        return self._normalize_timestamp_fields(
            {
                "status": "ok",
                "date_ist": date_ist,
                "rows": rows,
                "count": len(rows),
            }
        )

    async def get_live_strategy_trace_detail(self, trace_id: str) -> Any:
        service = self._require_live_strategy_monitor_service()
        try:
            payload = service.get_trace_detail(trace_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to load decision trace detail: {exc}")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=404, detail=f"decision trace '{trace_id}' not found")
        return self._normalize_timestamp_fields({"status": "ok", "trace": payload})

    async def health(self) -> dict[str, Any]:
        dashboard_page_ready = self._templates_dir.exists() and (self._templates_dir / "dashboard.html").exists()
        templates_ready = self._templates_dir.exists() and (self._templates_dir / "index.html").exists()
        live_strategy_page_ready = (self._templates_dir / "live_strategy.html").exists()
        strategy_eval_ready = self._get_strategy_eval_service() is not None
        live_strategy_ready = self._get_live_strategy_monitor_service() is not None
        market_data = self._probe_market_data_health()
        redis_status = self._probe_redis()

        ready = all(
            [
                dashboard_page_ready,
                strategy_eval_ready,
                live_strategy_ready,
                market_data["status"] == "healthy",
                redis_status["status"] == "healthy",
            ]
        )
        status = "healthy" if ready else "degraded"
        if not dashboard_page_ready:
            status = "unhealthy"

        return {
            "status": status,
            "ready": bool(ready),
            "service": "market-data-dashboard",
            "timestamp": self._now_iso_ist(),
            "checks": {
                "templates_dashboard": bool(dashboard_page_ready),
                "templates_index": bool(templates_ready),
                "templates_live_strategy": bool(live_strategy_page_ready),
                "strategy_evaluation_service": bool(strategy_eval_ready),
                "live_strategy_monitor_service": bool(live_strategy_ready),
                "market_data_api": market_data["status"] == "healthy",
                "redis": redis_status["status"] == "healthy",
            },
            "dependencies": {
                "market_data_api": market_data,
                "redis": redis_status,
                "strategy_evaluation_service": {
                    "status": "healthy" if strategy_eval_ready else "unavailable",
                },
                "live_strategy_monitor_service": {
                    "status": "healthy" if live_strategy_ready else "unavailable",
                },
            },
        }

    async def market_data_health(self) -> Any:
        result = self._request_market_data_json("health", timeout=5.0)
        payload = result.get("payload")
        if result["ok"] and isinstance(payload, dict):
            out = dict(payload)
            out["proxy_checked_at"] = self._now_iso_ist()
            out["upstream_url"] = result["url"]
            out["upstream_status_code"] = result["status_code"]
            out["upstream_latency_ms"] = result["latency_ms"]
            return out
        return {
            "status": "unhealthy",
            "error": result.get("error") or f"API returned status {result.get('status_code')}",
            "timestamp": self._now_iso_ist(),
            "upstream_url": result.get("url"),
            "upstream_status_code": result.get("status_code"),
            "upstream_latency_ms": result.get("latency_ms"),
            "upstream_payload": payload,
        }

    async def get_system_mode(self) -> Any:
        result = self._request_market_data_json("api/v1/system/mode", timeout=5.0)
        payload = result.get("payload")
        if result["ok"] and isinstance(payload, dict):
            return payload
        return {
            "mode": "unknown",
            "error": result.get("error") or f"API returned status {result.get('status_code')}",
            "timestamp": self._now_iso_ist(),
        }

    async def get_operator_halt(self) -> dict[str, Any]:
        halt_path = resolve_runtime_artifact_paths().operator_halt_path
        return {"halted": halt_path.exists(), "path": str(halt_path), "timestamp": self._now_iso_ist()}

    async def post_operator_halt(self) -> dict[str, Any]:
        halt_path = resolve_runtime_artifact_paths().operator_halt_path
        try:
            halt_path.parent.mkdir(parents=True, exist_ok=True)
            halt_path.touch()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to write operator halt signal: {exc}")
        return {"halted": True, "path": str(halt_path), "timestamp": self._now_iso_ist()}

    async def delete_operator_halt(self) -> dict[str, Any]:
        halt_path = resolve_runtime_artifact_paths().operator_halt_path
        try:
            halt_path.unlink(missing_ok=True)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to clear operator halt signal: {exc}")
        return {"halted": False, "path": str(halt_path), "timestamp": self._now_iso_ist()}
