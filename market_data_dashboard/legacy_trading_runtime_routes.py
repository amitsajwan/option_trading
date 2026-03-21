from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


logger = logging.getLogger(__name__)


class DashboardLegacyTradingRouter:
    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        repo_root: Path,
        ml_pipeline_src: Path,
        default_instrument: str,
        redis_host: str,
        redis_port: int,
        default_trading_events_path: Path,
        default_model_package: str,
        default_threshold_report: str,
        logger: Any,
        legacy_trading_runtime_status: Callable[[], dict[str, Any]],
        build_trading_model_catalog: Callable[[], list[dict[str, Any]]],
        normalize_trading_instance: Callable[[Any], str],
        resolve_repo_path: Callable[[Optional[str], Optional[Path]], Optional[Path]],
        coerce_float: Callable[[Any], Optional[float]],
        truthy: Callable[..., bool],
        now_ist: Callable[[], Any],
        json_safe_value: Callable[[Any], Any],
        save_latest_backtest_state: Callable[[str, dict[str, Any]], dict[str, Any]],
        load_latest_backtest_state: Callable[[str], Optional[dict[str, Any]]],
        trading_lock: Any,
        default_trading_paths: Callable[[str], tuple[Path, Path, Path]],
        refresh_trading_runner_state: Callable[[str], dict[str, Any]],
        stop_trading_process_locked: Callable[..., dict[str, Any]],
        close_trading_log_handles: Callable[[dict[str, Any]], None],
        load_runtime_instruments: Callable[..., Any],
        select_most_active_instrument: Callable[..., Any],
        is_placeholder_instrument: Callable[[Any], bool],
        load_trading_events: Callable[[Path, Optional[int]], list[dict[str, Any]]],
        build_trading_state: Callable[[list[dict[str, Any]]], dict[str, Any]],
        backtest_timeout_seconds: int = 1800,
    ) -> None:
        self._templates = templates
        self._repo_root = repo_root.resolve()
        self._ml_pipeline_src = ml_pipeline_src.resolve()
        self._default_instrument = str(default_instrument or "")
        self._redis_host = str(redis_host or "localhost")
        self._redis_port = int(redis_port)
        self._default_trading_events_path = default_trading_events_path.resolve()
        self._default_model_package = str(default_model_package or "")
        self._default_threshold_report = str(default_threshold_report or "")
        self._logger = logger
        self._legacy_trading_runtime_status = legacy_trading_runtime_status
        self._build_trading_model_catalog = build_trading_model_catalog
        self._normalize_trading_instance = normalize_trading_instance
        self._resolve_repo_path = resolve_repo_path
        self._coerce_float = coerce_float
        self._truthy = truthy
        self._now_ist = now_ist
        self._json_safe_value = json_safe_value
        self._save_latest_backtest_state = save_latest_backtest_state
        self._load_latest_backtest_state = load_latest_backtest_state
        self._trading_lock = trading_lock
        self._default_trading_paths = default_trading_paths
        self._refresh_trading_runner_state = refresh_trading_runner_state
        self._stop_trading_process_locked = stop_trading_process_locked
        self._close_trading_log_handles = close_trading_log_handles
        self._load_runtime_instruments = load_runtime_instruments
        self._select_most_active_instrument = select_most_active_instrument
        self._is_placeholder_instrument = is_placeholder_instrument
        self._load_trading_events = load_trading_events
        self._build_trading_state = build_trading_state
        self._backtest_timeout_seconds = max(1, int(backtest_timeout_seconds))

        router = APIRouter(tags=["legacy-trading"])
        router.add_api_route("/trading", self.trading_terminal, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/api/trading/backtest/run", self.run_trading_backtest, methods=["POST"])
        router.add_api_route("/api/trading/backtest/latest", self.get_latest_backtest_state, methods=["GET"])
        router.add_api_route("/api/trading/state", self.get_trading_state, methods=["GET"])
        router.add_api_route("/api/trading/start", self.start_trading_runner, methods=["POST"])
        router.add_api_route("/api/trading/stop", self.stop_trading_runner, methods=["POST"])
        self.router = router

    async def _read_request_payload(self, request: Request) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        try:
            body = await request.json()
            if isinstance(body, dict):
                payload = body
        except Exception:
            payload = {}
        return payload

    def _require_enabled(self, detail: Optional[str] = None) -> dict[str, Any]:
        runtime = self._legacy_trading_runtime_status()
        if not bool(runtime.get("enabled")):
            raise HTTPException(
                status_code=503,
                detail=str(detail or runtime.get("detail") or "legacy trading runtime unavailable"),
            )
        return runtime

    def _validate_thresholds(self, ce_threshold: Optional[float], pe_threshold: Optional[float]) -> None:
        if ce_threshold is not None and (ce_threshold < 0.0 or ce_threshold > 1.0):
            raise HTTPException(status_code=400, detail=f"ce_threshold must be within [0, 1], got {ce_threshold}")
        if pe_threshold is not None and (pe_threshold < 0.0 or pe_threshold > 1.0):
            raise HTTPException(status_code=400, detail=f"pe_threshold must be within [0, 1], got {pe_threshold}")

    def _resolve_workspace_path(self, raw: Any, *, default_path: Path, label: str) -> Path:
        text = str(raw).strip() if raw is not None else ""
        candidate = default_path if not text else Path(text)
        if not candidate.is_absolute():
            candidate = self._repo_root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self._repo_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{label} must stay under repo root: {self._repo_root}") from exc
        return candidate

    def _pythonpath_env(self, *, auto_refresh_vix: bool, vix_from_date: str) -> dict[str, str]:
        env = dict(os.environ)
        current_pythonpath = str(env.get("PYTHONPATH") or "").strip()
        env["PYTHONPATH"] = (
            f"{self._ml_pipeline_src}{os.pathsep}{current_pythonpath}"
            if current_pythonpath
            else str(self._ml_pipeline_src)
        )
        if auto_refresh_vix:
            env["ML_PIPELINE_AUTO_FETCH_VIX"] = "1"
            env["ML_PIPELINE_VIX_FROM_DATE"] = vix_from_date
        return env

    async def trading_terminal(self, request: Request) -> HTMLResponse:
        legacy_trading_runtime = self._legacy_trading_runtime_status()
        query = dict(request.query_params)
        model_key_raw = str(query.get("model") or "").strip()
        if model_key_raw:
            safe_key = self._normalize_trading_instance(model_key_raw)
            selected = next(
                (
                    item
                    for item in self._build_trading_model_catalog()
                    if str(item.get("instance_key") or "").strip().lower() == safe_key.lower()
                ),
                None,
            )
            if isinstance(selected, dict):
                merged = dict(query)
                changed = False
                for key in ("model_package", "threshold_report", "eval_summary_path", "training_report_path"):
                    if not str(merged.get(key) or "").strip():
                        value = str(selected.get(key) or "").strip()
                        if value:
                            merged[key] = value
                            changed = True
                merged["model"] = safe_key
                if changed or safe_key != model_key_raw:
                    return RedirectResponse(url=f"/trading?{urlencode(merged)}", status_code=307)
        return self._templates.TemplateResponse(
            "trading_terminal.html",
            {"request": request, "legacy_trading_runtime": legacy_trading_runtime},
        )

    async def get_latest_backtest_state(self, instance: Optional[str] = None) -> dict[str, Any]:
        self._require_enabled()
        instance_key = self._normalize_trading_instance(instance)
        latest = self._load_latest_backtest_state(instance_key)
        if not isinstance(latest, dict):
            return {"status": "not_found", "instance": instance_key}
        return {"status": "ok", **latest}

    async def get_trading_state(
        self,
        limit: int = 2000,
        instance: Optional[str] = None,
        view: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        instance_key = self._normalize_trading_instance(instance)
        with self._trading_lock:
            state = self._refresh_trading_runner_state(instance_key)
            process = state.get("process")
            runner_pid = process.pid if process is not None else None
            runner_running = process is not None and process.poll() is None
            started_at = state.get("started_at")
            last_exit = state.get("last_exit_code")
            runner_cfg = dict(state.get("config") or {})
            events_path = state.get("events_path")
            if not isinstance(events_path, Path):
                events_path = Path(str(events_path or self._default_trading_events_path))

        view_mode = str(view or "auto").strip().lower()
        if view_mode in {"backtest", "latest_backtest"}:
            latest = self._load_latest_backtest_state(instance_key)
            ui_state = latest.get("ui_state") if isinstance(latest, dict) else None
            if isinstance(ui_state, dict):
                return ui_state

        events = self._load_trading_events(events_path, limit=max(1, int(limit)))
        if (not runner_running) and len(events) == 0:
            latest = self._load_latest_backtest_state(instance_key)
            ui_state = latest.get("ui_state") if isinstance(latest, dict) else None
            if isinstance(ui_state, dict):
                return ui_state

        payload = self._build_trading_state(events)
        payload["runner"] = {
            "instance": instance_key,
            "running": runner_running,
            "pid": runner_pid,
            "started_at": started_at,
            "last_exit_code": last_exit,
            "config": runner_cfg,
            "events_path": str(events_path),
        }
        return payload

    async def stop_trading_runner(self, instance: Optional[str] = None) -> dict[str, Any]:
        self._require_enabled()
        instance_key = self._normalize_trading_instance(instance)
        with self._trading_lock:
            state = self._refresh_trading_runner_state(instance_key)
            if state.get("process") is None:
                return {"status": "not_running", "instance": instance_key, "last_exit_code": state.get("last_exit_code")}
            stop_meta = self._stop_trading_process_locked(state, reason="manual_stop")
            return {"status": "stopped", "instance": instance_key, "last_exit_code": stop_meta.get("last_exit_code")}

    async def run_trading_backtest(self, request: Request) -> Any:
        self._require_enabled()
        payload = await self._read_request_payload(request)

        backtest_date = str(payload.get("date") or "").strip()
        if not backtest_date:
            raise HTTPException(status_code=400, detail="date is required (YYYY-MM-DD)")
        try:
            datetime.strptime(backtest_date, "%Y-%m-%d")
        except Exception as exc:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc

        instrument = str(payload.get("instrument") or "").strip().upper()
        if not instrument:
            raise HTTPException(status_code=400, detail="instrument is required")

        model_path = self._resolve_repo_path(str(payload.get("model_package") or "").strip(), None)
        threshold_path = self._resolve_repo_path(str(payload.get("threshold_report") or "").strip(), None)
        ce_threshold = self._coerce_float(payload.get("ce_threshold"))
        pe_threshold = self._coerce_float(payload.get("pe_threshold"))
        if not isinstance(model_path, Path) or not model_path.exists():
            raise HTTPException(status_code=400, detail=f"model package not found: {model_path}")
        if not isinstance(threshold_path, Path) or not threshold_path.exists():
            raise HTTPException(status_code=400, detail=f"threshold report not found: {threshold_path}")
        self._validate_thresholds(ce_threshold, pe_threshold)

        source = str(payload.get("source") or "auto").strip().lower()
        if source not in {"auto", "local", "mongo"}:
            raise HTTPException(status_code=400, detail="source must be one of: auto, local, mongo")

        base_path = str(payload.get("base_path") or "").strip()
        mongo_uri = str(payload.get("mongo_uri") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017/").strip()
        mongo_db = str(payload.get("mongo_db") or os.getenv("MONGO_DB") or "trading_ai").strip()
        vix_path = str(payload.get("vix_path") or "").strip()
        t19_path = self._resolve_repo_path(str(payload.get("t19_report") or "").strip(), None) if payload.get("t19_report") else None
        out_dir = self._resolve_workspace_path(
            payload.get("out_dir") or ".run/dashboard_backtests",
            default_path=self._repo_root / ".run" / "dashboard_backtests",
            label="out_dir",
        )
        instance_key = self._normalize_trading_instance(payload.get("instance"))
        run_id = self._now_ist().strftime("%Y%m%d_%H%M%S")
        safe_instrument = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in instrument)
        run_tag = str(payload.get("tag") or f"{backtest_date}_{safe_instrument}_{run_id}")

        env = self._pythonpath_env(
            auto_refresh_vix=self._truthy(payload.get("auto_refresh_vix"), default=True),
            vix_from_date=str(payload.get("vix_from_date") or "2024-01-01").strip(),
        )
        cmd = [
            sys.executable, "-m", "ml_pipeline.date_backtest_runner",
            "--date", backtest_date,
            "--instrument", instrument,
            "--model-package", str(model_path),
            "--threshold-report", str(threshold_path),
            "--source", source,
            "--mongo-uri", mongo_uri,
            "--mongo-db", mongo_db,
            "--out-dir", str(out_dir),
            "--tag", run_tag,
        ]
        if base_path:
            cmd.extend(["--base-path", base_path])
        if vix_path:
            cmd.extend(["--vix-path", vix_path])
        if isinstance(t19_path, Path):
            cmd.extend(["--t19-report", str(t19_path)])
        if ce_threshold is not None:
            cmd.extend(["--ce-threshold", str(float(ce_threshold))])
        if pe_threshold is not None:
            cmd.extend(["--pe-threshold", str(float(pe_threshold))])

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self._repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=self._backtest_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(
                status_code=504,
                detail=f"backtest timed out after {self._backtest_timeout_seconds} seconds",
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            if len(detail) > 1800:
                detail = detail[-1800:]
            raise HTTPException(status_code=500, detail=f"backtest failed: {detail}")

        full_report_path: Optional[Path] = None
        for line in (proc.stdout or "").splitlines():
            text = str(line).strip()
            if text.startswith("FULL_REPORT="):
                candidate = text.split("=", 1)[1].strip()
                if candidate:
                    full_report_path = Path(candidate)
                    break
        if full_report_path is None:
            expected = out_dir / run_tag / "full_report.json"
            if expected.exists():
                full_report_path = expected
        if full_report_path is None or not full_report_path.exists():
            raise HTTPException(status_code=500, detail="backtest completed but full report was not found")

        try:
            result = json.loads(full_report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to parse backtest report: {exc}") from exc

        ui_state: dict[str, Any] = {}
        try:
            decisions_path_raw = ((result.get("artifacts") or {}).get("decisions_jsonl")) if isinstance(result, dict) else None
            decisions_path = Path(str(decisions_path_raw)) if decisions_path_raw else None
            if isinstance(decisions_path, Path) and decisions_path.exists():
                ui_state = self._build_trading_state(self._load_trading_events(decisions_path, limit=5000))
                ui_state["runner"] = {
                    "instance": instance_key,
                    "running": False,
                    "pid": None,
                    "started_at": None,
                    "last_exit_code": 0,
                    "config": {
                        "instrument": instrument,
                        "model_package": str(model_path),
                        "threshold_report": str(threshold_path),
                        "ce_threshold": float(ce_threshold) if ce_threshold is not None else None,
                        "pe_threshold": float(pe_threshold) if pe_threshold is not None else None,
                        "mode": "backtest",
                    },
                    "events_path": str(decisions_path),
                    "view_mode": "backtest",
                    "backtest_date": backtest_date,
                    "backtest_run_tag": run_tag,
                }
        except Exception:
            ui_state = {}

        response_payload = {
            "status": "ok",
            "instance": instance_key,
            "run_tag": run_tag,
            "report_path": str(full_report_path),
            "result": result,
            "ui_state": ui_state,
        }
        safe_payload = self._json_safe_value(response_payload)
        self._save_latest_backtest_state(
            instance_key,
            {
                "instance": instance_key,
                "run_tag": run_tag,
                "report_path": str(full_report_path),
                "created_at": self._now_ist().isoformat(),
                "ui_state": safe_payload.get("ui_state") if isinstance(safe_payload, dict) else {},
            },
        )
        return safe_payload

    async def start_trading_runner(self, request: Request) -> dict[str, Any]:
        self._require_enabled(
            "Legacy paper trading runner is not part of the supported Live+Dashboard profile. Use strategy_app deterministic/ml_pure runtime instead."
        )
        payload = await self._read_request_payload(request)
        instance = self._normalize_trading_instance(payload.get("instance"))

        mode = str(payload.get("mode") or "dual").strip().lower()
        if mode not in {"dual", "ce_only", "pe_only"}:
            raise HTTPException(status_code=400, detail="mode must be one of: dual, ce_only, pe_only")

        requested_instrument = str(payload.get("instrument") or "").strip().upper()
        if requested_instrument and not self._is_placeholder_instrument(requested_instrument):
            instrument = requested_instrument
        else:
            runtime_instruments = await self._load_runtime_instruments(max_instruments=25)
            if self._default_instrument and not self._is_placeholder_instrument(self._default_instrument):
                runtime_instruments = [self._default_instrument] + list(runtime_instruments or [])
            instrument = str(self._select_most_active_instrument(runtime_instruments, preferred_mode="live") or "").strip().upper()
            if not instrument:
                instrument = "BANKNIFTY-I"
            self._logger.info("[trading/start] Auto-selected instrument=%s (requested=%s)", instrument, requested_instrument or "<empty>")

        redis_host = str(payload.get("redis_host") or self._redis_host).strip()
        redis_port = int(payload.get("redis_port") or self._redis_port)
        redis_db = int(payload.get("redis_db") or 0)
        initial_ce_capital = float(payload.get("initial_ce_capital") or 1000.0)
        initial_pe_capital = float(payload.get("initial_pe_capital") or 1000.0)
        fee_bps = float(payload.get("fee_bps") or 5.0)
        max_iterations = int(payload.get("max_iterations") or 800)
        max_hold_minutes = int(payload.get("max_hold_minutes") or 5)
        confidence_buffer = float(payload.get("confidence_buffer") or 0.05)
        max_idle_seconds = float(payload.get("max_idle_seconds") or 300.0)
        stop_loss_pct = float(payload.get("stop_loss_pct") or 0.0)
        trailing_enabled = self._truthy(payload.get("trailing_enabled"), default=False)
        trailing_activation_pct = float(payload.get("trailing_activation_pct") or 10.0)
        trailing_offset_pct = float(payload.get("trailing_offset_pct") or 5.0)
        trailing_lock_breakeven = self._truthy(payload.get("trailing_lock_breakeven"), default=True)
        model_exit_policy = str(payload.get("model_exit_policy") or "strict").strip().lower()
        stagnation_enabled = self._truthy(payload.get("stagnation_enabled"), default=False)
        stagnation_window_minutes = int(10 if payload.get("stagnation_window_minutes") in (None, "") else payload.get("stagnation_window_minutes"))
        stagnation_threshold_pct = float(0.8 if payload.get("stagnation_threshold_pct") in (None, "") else payload.get("stagnation_threshold_pct"))
        stagnation_volatility_multiplier = float(2.0 if payload.get("stagnation_volatility_multiplier") in (None, "") else payload.get("stagnation_volatility_multiplier"))
        stagnation_min_hold_minutes = int(0 if payload.get("stagnation_min_hold_minutes") in (None, "") else payload.get("stagnation_min_hold_minutes"))
        stop_execution_mode = str(payload.get("stop_execution_mode") or "stop_market").strip().lower()
        stop_limit_offset_pct = float(payload.get("stop_limit_offset_pct") or 0.2)
        stop_limit_max_wait_events = int(payload.get("stop_limit_max_wait_events") or 3)
        runtime_guard_max_consecutive_losses = int(payload.get("runtime_guard_max_consecutive_losses") or 0)
        runtime_guard_max_drawdown_pct = float(payload.get("runtime_guard_max_drawdown_pct") or 0.0)
        quality_max_entries_per_day = int(payload.get("quality_max_entries_per_day") or 0)
        quality_entry_cutoff_hour = int(payload.get("quality_entry_cutoff_hour") or -1)
        quality_entry_cooldown_minutes = int(payload.get("quality_entry_cooldown_minutes") or 0)
        quality_min_side_prob = float(payload.get("quality_min_side_prob") or 0.0)
        quality_min_prob_edge = float(payload.get("quality_min_prob_edge") or 0.0)
        quality_skip_weekdays = str(payload.get("quality_skip_weekdays") or "")
        option_lot_size = float(payload.get("option_lot_size") or 15.0)
        fresh_start = self._truthy(payload.get("fresh_start"), default=True)
        restart_if_running = self._truthy(payload.get("restart_if_running"), default=True)

        if model_exit_policy not in {"strict", "signal_only", "stop_only", "training_parity"}:
            raise HTTPException(status_code=400, detail="model_exit_policy must be one of: strict, signal_only, stop_only, training_parity")
        if stop_execution_mode not in {"stop_market", "stop_limit"}:
            raise HTTPException(status_code=400, detail="stop_execution_mode must be one of: stop_market, stop_limit")

        default_events_path, default_stdout_path, default_stderr_path = self._default_trading_paths(instance)
        model_path = self._resolve_repo_path(str(payload.get("model_package") or "").strip(), Path(self._default_model_package))
        threshold_path = self._resolve_repo_path(str(payload.get("threshold_report") or "").strip(), Path(self._default_threshold_report))
        ce_threshold = self._coerce_float(payload.get("ce_threshold"))
        pe_threshold = self._coerce_float(payload.get("pe_threshold"))
        output_path = self._resolve_workspace_path(payload.get("output_jsonl"), default_path=default_events_path, label="output_jsonl")
        feature_trace_path = self._resolve_workspace_path(
            payload.get("feature_trace_jsonl"),
            default_path=output_path.parent / f"t33_paper_feature_trace_{instance}.jsonl",
            label="feature_trace_jsonl",
        )
        stdout_path = default_stdout_path
        stderr_path = default_stderr_path

        legacy_runner_note = (
            "This page launches the archived paper runner and requires legacy "
            "model_package + threshold_report artifacts. It does not use the "
            "live registry-backed strategy_app deployment."
        )
        if not isinstance(model_path, Path) or not model_path.exists():
            raise HTTPException(status_code=400, detail=f"{legacy_runner_note} Missing model package: {model_path}")
        if not isinstance(threshold_path, Path) or not threshold_path.exists():
            raise HTTPException(status_code=400, detail=f"{legacy_runner_note} Missing threshold report: {threshold_path}")
        self._validate_thresholds(ce_threshold, pe_threshold)

        requested_identity = {
            "instance": instance,
            "mode": mode,
            "instrument": instrument,
            "redis_host": redis_host,
            "redis_port": redis_port,
            "redis_db": redis_db,
            "initial_ce_capital": initial_ce_capital,
            "initial_pe_capital": initial_pe_capital,
            "fee_bps": fee_bps,
            "max_iterations": max_iterations,
            "max_hold_minutes": max_hold_minutes,
            "confidence_buffer": confidence_buffer,
            "max_idle_seconds": max_idle_seconds,
            "stop_loss_pct": stop_loss_pct,
            "trailing_enabled": trailing_enabled,
            "trailing_activation_pct": trailing_activation_pct,
            "trailing_offset_pct": trailing_offset_pct,
            "trailing_lock_breakeven": trailing_lock_breakeven,
            "model_exit_policy": model_exit_policy,
            "stagnation_enabled": bool(stagnation_enabled),
            "stagnation_window_minutes": max(2, int(stagnation_window_minutes)),
            "stagnation_threshold_pct": max(0.0, float(stagnation_threshold_pct)),
            "stagnation_volatility_multiplier": max(0.0, float(stagnation_volatility_multiplier)),
            "stagnation_min_hold_minutes": max(0, int(stagnation_min_hold_minutes)),
            "stop_execution_mode": stop_execution_mode,
            "stop_limit_offset_pct": max(0.0, float(stop_limit_offset_pct)),
            "stop_limit_max_wait_events": max(1, int(stop_limit_max_wait_events)),
            "runtime_guard_max_consecutive_losses": max(0, int(runtime_guard_max_consecutive_losses)),
            "runtime_guard_max_drawdown_pct": max(0.0, float(runtime_guard_max_drawdown_pct)),
            "quality_max_entries_per_day": max(0, int(quality_max_entries_per_day)),
            "quality_entry_cutoff_hour": int(quality_entry_cutoff_hour),
            "quality_entry_cooldown_minutes": max(0, int(quality_entry_cooldown_minutes)),
            "quality_min_side_prob": min(1.0, max(0.0, float(quality_min_side_prob))),
            "quality_min_prob_edge": min(1.0, max(0.0, float(quality_min_prob_edge))),
            "quality_skip_weekdays": quality_skip_weekdays,
            "option_lot_size": max(1.0, float(option_lot_size)),
            "model_package": str(model_path),
            "threshold_report": str(threshold_path),
            "ce_threshold": float(ce_threshold) if ce_threshold is not None else None,
            "pe_threshold": float(pe_threshold) if pe_threshold is not None else None,
            "output_jsonl": str(output_path),
            "feature_trace_jsonl": str(feature_trace_path),
        }

        with self._trading_lock:
            state = self._refresh_trading_runner_state(instance)
            current_process = state.get("process")
            restart_meta: Optional[dict[str, Any]] = None
            if current_process is not None and current_process.poll() is None:
                current_cfg = dict(state.get("config") or {})
                changed_keys = sorted([k for k, v in requested_identity.items() if current_cfg.get(k) != v])
                if not restart_if_running and len(changed_keys) == 0:
                    return {
                        "status": "already_running",
                        "instance": instance,
                        "pid": current_process.pid,
                        "events_path": str(state.get("events_path") or output_path),
                        "config": dict(state.get("config") or {}),
                    }
                restart_meta = self._stop_trading_process_locked(
                    state,
                    reason="restart_with_new_config" if len(changed_keys) > 0 else "restart_requested",
                )
                restart_meta["changed_keys"] = changed_keys
                state = self._refresh_trading_runner_state(instance)
                current_process = state.get("process")

            if current_process is not None and current_process.poll() is None:
                return {
                    "status": "already_running",
                    "instance": instance,
                    "pid": current_process.pid,
                    "events_path": str(state.get("events_path") or output_path),
                    "config": dict(state.get("config") or {}),
                }

            output_path.parent.mkdir(parents=True, exist_ok=True)
            feature_trace_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            if fresh_start:
                for path in (output_path, stdout_path, stderr_path, feature_trace_path):
                    try:
                        if path.exists():
                            path.unlink()
                    except Exception:
                        pass

            env = self._pythonpath_env(
                auto_refresh_vix=self._truthy(payload.get("auto_refresh_vix"), default=True),
                vix_from_date=str(payload.get("vix_from_date") or "2024-01-01").strip(),
            )
            cmd = [
                sys.executable,
                "-m",
                "ml_pipeline.paper_capital_runner",
                "--mode",
                mode,
                "--instrument",
                instrument,
                "--model-package",
                str(model_path),
                "--threshold-report",
                str(threshold_path),
                "--redis-host",
                redis_host,
                "--redis-port",
                str(redis_port),
                "--redis-db",
                str(redis_db),
                "--initial-ce-capital",
                str(initial_ce_capital),
                "--initial-pe-capital",
                str(initial_pe_capital),
                "--fee-bps",
                str(fee_bps),
                "--max-iterations",
                str(max_iterations),
                "--max-hold-minutes",
                str(max_hold_minutes),
                "--confidence-buffer",
                str(confidence_buffer),
                "--max-idle-seconds",
                str(max_idle_seconds),
                "--stop-loss-pct",
                str(max(0.0, float(stop_loss_pct))),
                "--trailing-activation-pct",
                str(max(0.0, float(trailing_activation_pct))),
                "--trailing-offset-pct",
                str(max(0.0, float(trailing_offset_pct))),
                "--model-exit-policy",
                model_exit_policy,
                "--stagnation-window-minutes",
                str(max(2, int(stagnation_window_minutes))),
                "--stagnation-threshold-pct",
                str(max(0.0, float(stagnation_threshold_pct))),
                "--stagnation-volatility-multiplier",
                str(max(0.0, float(stagnation_volatility_multiplier))),
                "--stagnation-min-hold-minutes",
                str(max(0, int(stagnation_min_hold_minutes))),
                "--stop-execution-mode",
                stop_execution_mode,
                "--stop-limit-offset-pct",
                str(max(0.0, float(stop_limit_offset_pct))),
                "--stop-limit-max-wait-events",
                str(max(1, int(stop_limit_max_wait_events))),
                "--runtime-guard-max-consecutive-losses",
                str(max(0, int(runtime_guard_max_consecutive_losses))),
                "--runtime-guard-max-drawdown-pct",
                str(max(0.0, float(runtime_guard_max_drawdown_pct))),
                "--quality-max-entries-per-day",
                str(max(0, int(quality_max_entries_per_day))),
                "--quality-entry-cutoff-hour",
                str(int(quality_entry_cutoff_hour)),
                "--quality-entry-cooldown-minutes",
                str(max(0, int(quality_entry_cooldown_minutes))),
                "--quality-min-side-prob",
                str(min(1.0, max(0.0, float(quality_min_side_prob)))),
                "--quality-min-prob-edge",
                str(min(1.0, max(0.0, float(quality_min_prob_edge)))),
                "--quality-skip-weekdays",
                quality_skip_weekdays,
                "--option-lot-size",
                str(max(1.0, float(option_lot_size))),
                "--output-jsonl",
                str(output_path),
                "--feature-trace-jsonl",
                str(feature_trace_path),
            ]
            if ce_threshold is not None:
                cmd.extend(["--ce-threshold", str(float(ce_threshold))])
            if pe_threshold is not None:
                cmd.extend(["--pe-threshold", str(float(pe_threshold))])
            if bool(trailing_enabled):
                cmd.append("--trailing-enabled")
            if not bool(trailing_lock_breakeven):
                cmd.append("--no-trailing-lock-breakeven")
            if bool(stagnation_enabled):
                cmd.append("--stagnation-enabled")

            self._close_trading_log_handles(state)
            state["stdout_handle"] = open(stdout_path, "a", encoding="utf-8")
            state["stderr_handle"] = open(stderr_path, "a", encoding="utf-8")
            process = subprocess.Popen(
                cmd,
                cwd=str(self._repo_root),
                env=env,
                stdout=state["stdout_handle"],
                stderr=state["stderr_handle"],
            )
            state["process"] = process
            state["started_at"] = self._now_ist().isoformat()
            state["last_exit_code"] = None
            state["events_path"] = output_path
            state["stdout_path"] = stdout_path
            state["stderr_path"] = stderr_path
            state["config"] = dict(requested_identity)

            response_payload: dict[str, Any] = {
                "status": "restarted" if restart_meta else "started",
                "instance": instance,
                "pid": process.pid,
                "started_at": state.get("started_at"),
                "events_path": str(output_path),
                "config": dict(state.get("config") or {}),
            }
            if restart_meta:
                response_payload["restart"] = restart_meta
            return response_payload

