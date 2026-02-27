"""Unified runtime helpers for market_data.

Provides:
- process helpers used by runner
- mode-aware historical replay runner
- credential resolution for Zerodha integrations
"""
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
from typing import Optional, Dict, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from kiteconnect import KiteConnect

from .env_settings import redis_config, resolve_instrument_symbol
from .kite_client import create_kite_client

PYTHON = sys.executable


def _sanitize_for_console(s: str) -> str:
    """Make string safe for consoles that cannot handle Unicode (replace non-ascii)."""
    try:
        return s.encode("ascii", "replace").decode("ascii")
    except Exception:
        return "".join((c if ord(c) < 128 else "?" for c in s))


def start_process(name: str, cmd: list[str], env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> subprocess.Popen:
    """Start a subprocess with a normalized PYTHONPATH and optional env."""
    try:
        print(_sanitize_for_console(f"   [START] Starting {name} -> {cmd}"))
    except Exception:
        try:
            print(f"   [START] Starting {name}")
        except Exception:
            pass

    if env is None:
        env = os.environ.copy()

    pythonpath = env.get("PYTHONPATH", "")
    to_add = ["./market_data/src"]
    for p in to_add:
        if p not in pythonpath:
            pythonpath = f"{pythonpath}{os.pathsep}{p}" if pythonpath else p
    env["PYTHONPATH"] = pythonpath

    proc = subprocess.Popen(cmd, env=env, cwd=cwd or os.getcwd())
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
    """Poll Redis for the historical readiness key set by the historical runner."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if redis_client.get("system:historical:data_ready"):
                return True
        except Exception:
            pass
        time.sleep(poll_interval)
    return False


def check_zerodha_credentials(prompt_login: bool = False, prompt_login_timeout: int = 300) -> Tuple[bool, Optional[str]]:
    """Check if Zerodha credentials or a valid token are available.

    If `prompt_login` is True and credentials are missing/invalid, this function
    will try to run an interactive login using `AuthStartup.trigger_interactive_login`.

    Returns (True, None) if credentials are valid, otherwise (False, message).
    """
    try:
        # Prefer KiteAuthService first to keep checks deterministic and easy to mock in tests.
        try:
            # Reuse existing credentials/token once obtained. Avoid repeated remote
            # validation calls during startup unless explicitly requested.
            strict_validate = os.getenv("KITE_REQUIRE_REMOTE_TOKEN_VALIDATION", "0").strip().lower() in ("1", "true", "yes")
            creds, svc = _load_credentials_from_candidates(require_valid=strict_validate)
            if creds:
                return True, None

            api_key = os.getenv("KITE_API_KEY")
            access_token = os.getenv("KITE_ACCESS_TOKEN")
            if api_key and access_token:
                if strict_validate:
                    test_creds = {"api_key": api_key, "data": {"access_token": access_token}}
                    if svc.is_token_valid(test_creds):
                        return True, None
                else:
                    return True, None

            if prompt_login and hasattr(svc, "trigger_interactive_login"):
                try:
                    success = svc.trigger_interactive_login(timeout=prompt_login_timeout)
                    if success:
                        # Re-check across all known credential locations after login.
                        creds, _ = _load_credentials_from_candidates(require_valid=strict_validate)
                        if creds:
                            return True, None
                        return True, None
                except Exception as e:
                    return False, f"Interactive login failed: {e}"

            return False, "No valid credentials found in credentials.json or environment"
        except ImportError:
            pass

        # Fallback: centralized AuthStartup module.
        from market_data.tools.auth_startup import AuthStartup

        auth = AuthStartup()
        success, message = auth.startup_check(allow_interactive=prompt_login)
        if success:
            return True, None
        if prompt_login and auth.trigger_interactive_login(timeout=prompt_login_timeout):
            return True, None
        return False, message
    except Exception as e:
        return False, str(e)


def build_collector_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build env for collectors, including Kite credentials if available.

    Prefer credentials.json over ambient environment values to avoid stale
    dotenv tokens overriding freshly-authenticated credentials.
    """
    env = (base_env or os.environ.copy()).copy()
    try:
        creds, _ = _load_credentials_from_candidates(require_valid=False)
        api_key, access_token, _ = _resolve_kite_credentials(creds=creds, prefer_env=False)
        if api_key and access_token:
            env["KITE_API_KEY"] = api_key
            env["KITE_ACCESS_TOKEN"] = access_token
    except Exception:
        pass
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
    """Resolve historical replay config from args and env."""
    source = historical_source or os.getenv("HISTORICAL_SOURCE") or "synthetic"
    speed = float(historical_speed or os.getenv("HISTORICAL_SPEED") or 1.0)
    start_date = historical_from or os.getenv("HISTORICAL_FROM")
    ticks = bool(historical_ticks if historical_ticks is not None else os.getenv("HISTORICAL_TICKS", "0") in ("1", "true", "yes"))
    return HistoricalReplayConfig(source=source, speed=speed, start_date=start_date, ticks=ticks)


def _parse_start_date(start_date: Optional[str]) -> Optional[datetime]:
    if not start_date:
        return None
    try:
        # Interpret YYYY-MM-DD as the trading session start (09:15) by default.
        # This avoids synthetic replays starting at midnight.
        d = datetime.strptime(start_date, "%Y-%m-%d")
        return d.replace(hour=9, minute=15, second=0, microsecond=0)
    except Exception:
        return None


def _credential_path_candidates() -> list[Path]:
    """Resolve possible credentials.json locations in priority order."""
    runtime_file = Path(__file__).resolve()
    paths: list[Path] = []

    # Highest priority: explicit path override.
    configured = (os.getenv("KITE_CREDENTIALS_PATH") or "").strip()
    if configured:
        paths.append(Path(configured))

    # Common runtime cwd behavior (used by live/supervisor flows).
    paths.append(Path.cwd() / "credentials.json")

    # Canonical repo path used by this project: trading_ai/credentials.json.
    paths.append(runtime_file.parents[3] / "credentials.json")

    # Backward-compatible legacy path: trading_ai/market_data/credentials.json.
    paths.append(runtime_file.parents[2] / "credentials.json")

    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _create_kite_auth_service(cred_path: Optional[Path] = None):
    """Instantiate KiteAuthService, supporting test doubles with no ctor args."""
    from market_data.tools.kite_auth_service import KiteAuthService

    if cred_path is None:
        return KiteAuthService()
    try:
        return KiteAuthService(str(cred_path))
    except TypeError:
        return KiteAuthService()


def _extract_api_key_and_token(creds: Optional[Dict[str, object]]) -> tuple[str, str]:
    """Extract api_key and access_token from credentials payload."""
    if not creds:
        return "", ""
    api_key = str((creds or {}).get("api_key") or "")
    data_obj = (creds or {}).get("data")
    data_token = ""
    if isinstance(data_obj, dict):
        data_token = str(data_obj.get("access_token") or "")
    access_token = str((creds or {}).get("access_token") or data_token or "")
    return api_key, access_token


def _resolve_kite_credentials(
    creds: Optional[Dict[str, object]] = None,
    *,
    prefer_env: bool = False,
) -> tuple[str, str, str]:
    """Resolve Kite credentials from file payload and environment.

    Returns:
      (api_key, access_token, source) where source is one of:
      - "credentials": resolved from credentials.json payload
      - "env": resolved from environment variables
      - "none": no complete pair found
    """
    file_api_key, file_access_token = _extract_api_key_and_token(creds)
    env_api_key = str(os.getenv("KITE_API_KEY") or "")
    env_access_token = str(os.getenv("KITE_ACCESS_TOKEN") or "")

    if prefer_env and env_api_key and env_access_token:
        return env_api_key, env_access_token, "env"
    if file_api_key and file_access_token:
        return file_api_key, file_access_token, "credentials"
    if env_api_key and env_access_token:
        return env_api_key, env_access_token, "env"
    return "", "", "none"


def _load_credentials_from_candidates(require_valid: bool = False) -> tuple[Optional[Dict[str, object]], object]:
    """Load credentials from known locations, optionally requiring a valid token."""
    for cred_path in _credential_path_candidates():
        try:
            svc = _create_kite_auth_service(cred_path)
            creds = svc.load_credentials()
            if not creds:
                continue
            api_key, access_token = _extract_api_key_and_token(creds)
            if not api_key or not access_token:
                continue
            if require_valid and not svc.is_token_valid(creds):
                continue
            return creds, svc
        except Exception:
            continue
    return None, _create_kite_auth_service()


def _resolve_kite_instance_for_historical() -> Optional["KiteConnect"]:
    """Resolve a KiteConnect instance for real Zerodha historical replay.

    Fail-fast policy: when the user asked for real Zerodha data, we should not
    silently fall back to synthetic/mock data.

    Returns:
        KiteConnect instance

    Raises:
        RuntimeError if kiteconnect isn't installed or credentials are missing/invalid.
    """
    try:
        from kiteconnect import KiteConnect
    except Exception as e:
        raise RuntimeError(
            "kiteconnect package is required for Zerodha historical replay. "
            "Install it in the active venv (pip install kiteconnect). "
            f"Import error: {e}"
        )

    # Optional auth precheck (helps produce actionable messages).
    try:
        from market_data.tools.auth_startup import AuthStartup

        run_precheck = os.getenv("KITE_AUTH_STARTUP_PRECHECK", "0").strip().lower() in ("1", "true", "yes")
        if run_precheck:
            auth = AuthStartup()
            # Precheck only: never block startup with interactive browser flow here.
            success, message = auth.startup_check(allow_interactive=False)
            if not success:
                print(f"   [WARNING] Auth check failed: {message}")
    except Exception:
        pass

    # Reuse token once authenticated; avoid repeated remote validation at startup.
    creds, _ = _load_credentials_from_candidates(require_valid=False)

    api_key, access_token, _ = _resolve_kite_credentials(creds=creds, prefer_env=False)

    if not api_key or not access_token:
        raise RuntimeError(
            "Missing Zerodha credentials for historical replay. "
            "Provide credentials.json or set KITE_API_KEY and KITE_ACCESS_TOKEN. "
            "You can generate credentials using: python -m market_data.tools.kite_auth"
        )

    try:
        kite_instance = create_kite_client(api_key=api_key, access_token=access_token)
        print("   [OK] KiteConnect instance created for historical data")
        return kite_instance
    except Exception as e:
        raise RuntimeError(f"Failed to create KiteConnect instance: {e}")


def _classify_kite_error(exc: Exception) -> str:
    """Classify Kite errors for actionable startup messaging."""
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


def kite_startup_preflight(attempts: int = 2, base_delay_sec: float = 1.0) -> Tuple[bool, str, str]:
    """Run a minimal Kite API preflight and classify failures.

    Returns:
      (ok, reason, detail) where reason is one of: ok, credential, network, unknown.
    """
    creds, _ = _load_credentials_from_candidates(require_valid=False)
    api_key, access_token, _ = _resolve_kite_credentials(creds=creds, prefer_env=False)

    if not api_key or not access_token:
        return False, "credential", "Missing KITE_API_KEY/KITE_ACCESS_TOKEN (or credentials.json token)"

    last_exc: Optional[Exception] = None
    last_reason = "unknown"
    max_attempts = max(1, int(attempts))

    for attempt in range(1, max_attempts + 1):
        try:
            kite = create_kite_client(api_key=api_key, access_token=access_token)
            # Official quick-check endpoint from Kite docs usage pattern.
            kite.profile()
            return True, "ok", "Kite profile check passed"
        except Exception as exc:
            last_exc = exc
            last_reason = _classify_kite_error(exc)
            if last_reason == "credential":
                return False, "credential", f"Kite credentials invalid: {exc}"
            if last_reason == "network" and attempt < max_attempts:
                time.sleep(base_delay_sec * attempt)
                continue
            break

    detail = str(last_exc) if last_exc else "Unknown preflight failure"
    return False, last_reason, detail


async def monitor_for_ticks(redis_client, timeout: int = 60, interval: int = 1) -> bool:
    """Poll Redis for tick keys (mode-aware) and set a readiness key when data appears."""

    # Prefer mode-prefixed websocket latest keys (e.g., historical:websocket:tick:*:latest)
    # to match the active publisher path.
    prefixed_pattern = None
    exec_mode = (os.getenv("EXECUTION_MODE") or "").strip().lower()
    try:
        from redis_key_manager import get_redis_pattern

        prefixed_pattern = get_redis_pattern("websocket:tick:*:latest")
    except Exception:
        if exec_mode in {"live", "historical", "paper"}:
            prefixed_pattern = f"{exec_mode}:websocket:tick:*:latest"
        else:
            prefixed_pattern = None

    raw_pattern = "websocket:tick:*:latest"
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        try:
            keys = []
            if prefixed_pattern:
                keys = redis_client.keys(prefixed_pattern) or []
            # Do not fall back to raw keys in explicit historical mode; raw/live keys
            # can produce false readiness and mask ingestion failures.
            if not keys and exec_mode != "historical":
                keys = redis_client.keys(raw_pattern) or []

            if keys:
                redis_client.set("system:historical:data_ready", "1")
                return True
        except Exception:
            pass
        await asyncio.sleep(interval)
    return False


async def run_historical_replay(config: HistoricalReplayConfig) -> None:
    """Run historical replay in-process using the resolved config."""
    from market_data.api import build_store, build_historical_replay

    try:
        import redis
    except Exception as e:
        raise RuntimeError(f"Redis dependency missing: {e}")

    redis_client = redis.Redis(**redis_config(decode_responses=True))

    store = build_store(redis_client=redis_client)

    start_date_obj = _parse_start_date(config.start_date)
    if config.start_date and not start_date_obj:
        print(f"   [WARNING] Invalid date format '{config.start_date}'. Expected YYYY-MM-DD. Using default.")

    kite_instance = None
    if config.source == "zerodha":
        pre_ok, pre_reason, pre_detail = kite_startup_preflight(attempts=2, base_delay_sec=1.0)
        if not pre_ok:
            if pre_reason == "network":
                raise RuntimeError(
                    f"Kite preflight failed: Network/TLS to api.kite.trade is unstable. Detail: {pre_detail}"
                )
            if pre_reason == "credential":
                raise RuntimeError(
                    f"Kite preflight failed: Invalid/expired api_key or access_token. Detail: {pre_detail}"
                )
            raise RuntimeError(f"Kite preflight failed: {pre_detail}")

        # Fail-fast: do not fall back to synthetic.
        kite_instance = _resolve_kite_instance_for_historical()

    print(
        f"Starting historical replay (source={config.source}, speed={config.speed}, from={config.start_date}, ticks={config.ticks})"
    )

    instrument_symbol = resolve_instrument_symbol()
    replay = build_historical_replay(
        store=store,
        data_source=config.source,
        start_date=start_date_obj,
        kite=kite_instance,
        speed=config.speed,
        instrument_symbol=instrument_symbol,
    )

    if not replay:
        print("   [ERROR] Failed to create replay instance")
        return

    replay.start()
    print("Historical replay started")

    try:
        redis_client.set("system:historical:running", "1")
    except Exception:
        pass

    # Fail-fast if no data shows up.
    # For real Zerodha, we expect ticks/ohlc to appear in Redis promptly.
    ready_timeout = int(os.getenv("HISTORICAL_READY_TIMEOUT", "60"))
    monitor_task = asyncio.create_task(monitor_for_ticks(redis_client, timeout=ready_timeout, interval=1))

    try:
        # Wait for readiness or premature replay stop.
        while True:
            if monitor_task.done():
                ok = bool(monitor_task.result())
                if not ok:
                    raise RuntimeError(
                        f"Historical replay did not produce any ticks in Redis within {ready_timeout}s. "
                        "For Zerodha, this usually means invalid credentials/token, market holiday, or wrong instrument."
                    )
                break

            if getattr(replay, "last_error", None) is not None:
                raise RuntimeError(f"Historical replay failed early: {getattr(replay, 'last_error')}")

            # If the replayer stops before producing data, exit non-zero.
            if hasattr(replay, "running") and not getattr(replay, "running"):
                raise RuntimeError("Historical replay stopped before producing any data")

            await asyncio.sleep(0.5)

        # Keep process alive while replay runs; if it dies, treat as an error for real sources.
        while True:
            if getattr(replay, "last_error", None) is not None:
                raise RuntimeError(f"Historical replay failed: {getattr(replay, 'last_error')}")
            if hasattr(replay, "running") and not getattr(replay, "running"):
                raise RuntimeError("Historical replay stopped unexpectedly")
            await asyncio.sleep(1)
    finally:
        try:
            replay.stop()
        except Exception:
            pass
        try:
            redis_client.delete("system:historical:running")
            redis_client.delete("system:historical:data_ready")
        except Exception:
            pass
