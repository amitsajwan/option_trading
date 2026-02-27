"""Unified runtime helpers for ingestion_app."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from .env_settings import credentials_path_candidates, redis_config
from .kite_client import create_kite_client

PYTHON = sys.executable


def _sanitize_for_console(s: str) -> str:
    try:
        return s.encode("ascii", "replace").decode("ascii")
    except Exception:
        return "".join((c if ord(c) < 128 else "?" for c in s))


def start_process(name: str, cmd: list[str], env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> subprocess.Popen:
    try:
        print(_sanitize_for_console(f"   [START] Starting {name} -> {cmd}"))
    except Exception:
        pass
    proc = subprocess.Popen(cmd, env=env or os.environ.copy(), cwd=cwd or os.getcwd())
    time.sleep(1)
    return proc


def wait_for_http(url: str, timeout: int = 30, retry_delay: int = 1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import requests

            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(retry_delay)
    return False


def wait_for_historical_ready(redis_client, timeout: int = 60, poll_interval: int = 1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if redis_client.get("system:historical:data_ready"):
                return True
        except Exception:
            pass
        time.sleep(poll_interval)
    return False


def _extract_api_key_and_token(payload: Optional[dict]) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    api_key = str(payload.get("api_key") or "").strip()
    access_token = str(
        payload.get("access_token")
        or ((payload.get("data") or {}).get("access_token") if isinstance(payload.get("data"), dict) else "")
        or ""
    ).strip()
    return api_key, access_token


def _load_credentials_from_candidates() -> Optional[dict]:
    for path in credentials_path_candidates():
        try:
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        api_key, access_token = _extract_api_key_and_token(payload)
        if api_key and access_token:
            return payload
    return None


def _resolve_kite_credentials(*, prefer_env: bool = False) -> tuple[str, str, str]:
    creds = _load_credentials_from_candidates()
    file_api_key, file_access_token = _extract_api_key_and_token(creds)
    env_api_key = str(os.getenv("KITE_API_KEY") or "").strip()
    env_access_token = str(os.getenv("KITE_ACCESS_TOKEN") or "").strip()
    if prefer_env and env_api_key and env_access_token:
        return env_api_key, env_access_token, "env"
    if file_api_key and file_access_token:
        return file_api_key, file_access_token, "credentials"
    if env_api_key and env_access_token:
        return env_api_key, env_access_token, "env"
    return "", "", "none"


def _classify_kite_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if (
        "incorrect `api_key` or `access_token`" in msg
        or "tokenexception" in msg
        or "invalid token" in msg
        or "permission denied" in msg
    ):
        return "credential"
    if (
        "httpsconnectionpool" in msg
        or "ssleoferror" in msg
        or "unexpected_eof_while_reading" in msg
        or "max retries exceeded" in msg
        or "connection reset" in msg
        or "timed out" in msg
    ):
        return "network"
    return "unknown"


def check_zerodha_credentials(prompt_login: bool = False, prompt_login_timeout: int = 300) -> Tuple[bool, Optional[str]]:
    del prompt_login_timeout
    api_key, access_token, source = _resolve_kite_credentials(prefer_env=False)
    if not api_key or not access_token:
        return False, "No valid credentials found in credentials.json or environment"
    # Fail-closed + manual token runbook. Interactive login is intentionally not automated.
    if prompt_login:
        return True, None
    return True, None


def build_collector_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = (base_env or os.environ.copy()).copy()
    api_key, access_token, source = _resolve_kite_credentials(prefer_env=False)
    if source in {"credentials", "env"} and api_key and access_token:
        env["KITE_API_KEY"] = api_key
        env["KITE_ACCESS_TOKEN"] = access_token
    return env


@dataclass
class HistoricalReplayConfig:
    source: str
    speed: float
    start_date: Optional[str]
    ticks: bool


def resolve_historical_replay_config(
    historical_source: Optional[str] = None,
    historical_speed: Optional[float] = None,
    historical_from: Optional[str] = None,
    historical_ticks: Optional[bool] = None,
) -> HistoricalReplayConfig:
    source = historical_source or os.getenv("HISTORICAL_SOURCE") or "synthetic"
    speed = float(historical_speed or os.getenv("HISTORICAL_SPEED") or 1.0)
    start_date = historical_from or os.getenv("HISTORICAL_FROM")
    ticks = bool(historical_ticks if historical_ticks is not None else os.getenv("HISTORICAL_TICKS", "0").strip().lower() in ("1", "true", "yes"))
    return HistoricalReplayConfig(source=source, speed=speed, start_date=start_date, ticks=ticks)


def _resolve_kite_instance_for_historical():
    try:
        import kiteconnect  # noqa: F401
    except Exception as e:
        raise RuntimeError(
            "kiteconnect package is required for Zerodha historical replay. "
            "Install it in the active venv (pip install kiteconnect). "
            f"Import error: {e}"
        )

    api_key, access_token, _ = _resolve_kite_credentials(prefer_env=False)
    if not api_key or not access_token:
        raise RuntimeError(
            "Missing Zerodha credentials for historical replay. "
            "Provide credentials.json or set KITE_API_KEY and KITE_ACCESS_TOKEN."
        )
    return create_kite_client(api_key=api_key, access_token=access_token)


def kite_startup_preflight(attempts: int = 2, base_delay_sec: float = 1.0) -> Tuple[bool, str, str]:
    api_key, access_token, _ = _resolve_kite_credentials(prefer_env=False)
    if not api_key or not access_token:
        return False, "credential", "Missing KITE_API_KEY/KITE_ACCESS_TOKEN (or credentials.json token)"

    last_exc: Optional[Exception] = None
    last_reason = "unknown"
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            kite = create_kite_client(api_key=api_key, access_token=access_token)
            kite.profile()
            return True, "ok", "Kite profile check passed"
        except Exception as exc:
            last_exc = exc
            last_reason = _classify_kite_error(exc)
            if last_reason == "credential":
                return False, "credential", f"Kite credentials invalid: {exc}"
            if last_reason == "network" and attempt < attempts:
                time.sleep(base_delay_sec * attempt)
                continue
            break
    detail = str(last_exc) if last_exc else "Unknown preflight failure"
    return False, last_reason, detail


async def monitor_for_ticks(redis_client, timeout: int = 60, interval: int = 1) -> bool:
    prefixed_pattern = None
    exec_mode = (os.getenv("EXECUTION_MODE") or "").strip().lower()
    try:
        from redis_key_manager import get_redis_pattern

        prefixed_pattern = get_redis_pattern("websocket:tick:*:latest")
    except Exception:
        if exec_mode in {"live", "historical", "paper"}:
            prefixed_pattern = f"{exec_mode}:websocket:tick:*:latest"
    raw_pattern = "websocket:tick:*:latest"
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        try:
            keys = redis_client.keys(prefixed_pattern) if prefixed_pattern else []
            if not keys and exec_mode != "historical":
                keys = redis_client.keys(raw_pattern)
            if keys:
                redis_client.set("system:historical:data_ready", "1")
                return True
        except Exception:
            pass
        await asyncio.sleep(interval)
    return False


async def run_historical_replay(config: HistoricalReplayConfig) -> None:
    del config
    # Historical replay was moved to snapshot_app.historical.replay_runner for clean layering.
    # Keep this explicit to avoid silent synthetic fallbacks.
    raise RuntimeError(
        "ingestion_app historical replay is deprecated. "
        "Use: python -m snapshot_app.historical.replay_runner"
    )
