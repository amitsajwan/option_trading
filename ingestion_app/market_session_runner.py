from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from contracts_app import is_market_open_ist, is_trading_day_ist, load_holidays, seconds_until_next_open_ist

from .runtime import check_zerodha_credentials, kite_startup_preflight

logger = logging.getLogger(__name__)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_iso(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


@dataclass
class RunnerConfig:
    mode: str
    session_enabled: bool
    timezone_name: str
    market_open_time: str
    market_close_time: str
    holidays_file: str
    idle_sleep_seconds: int
    open_retry_seconds: int
    state_file: Path
    skip_validation: bool


def _now_ist(tz_name: str) -> datetime:
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = ZoneInfo("Asia/Kolkata")
    return datetime.now(tz=zone)


def _state_payload(
    *,
    cfg: RunnerConfig,
    status: str,
    child_proc: Optional[subprocess.Popen],
    market_open: bool,
    trading_day: bool,
    reason: Optional[str],
) -> dict:
    now = _now_ist(cfg.timezone_name)
    return {
        "component": "ingestion_app.market_session_runner",
        "checked_at_ist": now.isoformat(),
        "status": status,
        "market_open": bool(market_open),
        "trading_day": bool(trading_day),
        "idle_reason": reason,
        "collector_pid": int(child_proc.pid) if child_proc is not None and child_proc.poll() is None else None,
        "market_session_enabled": cfg.session_enabled,
        "timezone": cfg.timezone_name,
        "open_time": cfg.market_open_time,
        "close_time": cfg.market_close_time,
        "holidays_file": cfg.holidays_file,
    }


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _stop_child(child_proc: Optional[subprocess.Popen]) -> None:
    if child_proc is None or child_proc.poll() is not None:
        return
    try:
        child_proc.terminate()
        child_proc.wait(timeout=20)
    except Exception:
        try:
            child_proc.kill()
        except Exception:
            pass


def _start_child(cfg: RunnerConfig) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "ingestion_app.runner", "--mode", cfg.mode, "--start-collectors"]
    if cfg.skip_validation:
        cmd.append("--skip-validation")
    logger.info("starting ingestion runner command=%s", cmd)
    return subprocess.Popen(cmd, env=None, stdin=subprocess.DEVNULL)


def _preflight_credentials() -> tuple[bool, str]:
    ok, message = check_zerodha_credentials(prompt_login=False)
    if not ok:
        return False, f"credentials_missing_or_invalid: {message or 'no details'}"
    pre_ok, pre_reason, pre_detail = kite_startup_preflight(attempts=2, base_delay_sec=1.0)
    if not pre_ok:
        return False, f"kite_preflight_{pre_reason}: {pre_detail}"
    return True, "ok"


def _run_healthcheck(*, state_file: Path, max_stale_seconds: float) -> int:
    payload = {}
    if state_file.exists():
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    checked_at = _parse_iso(str(payload.get("checked_at_ist") or ""))
    age_seconds = None
    status = "healthy"
    if checked_at is None:
        status = "unhealthy"
    else:
        now = datetime.now(tz=checked_at.tzinfo or ZoneInfo("Asia/Kolkata"))
        age_seconds = (now - checked_at).total_seconds()
        if age_seconds > float(max_stale_seconds):
            status = "unhealthy"
    payload["healthcheck_status"] = status
    payload["healthcheck_age_seconds"] = round(float(age_seconds), 3) if age_seconds is not None else None
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if status == "healthy" else 2


def _loop(cfg: RunnerConfig) -> int:
    shutdown_requested = False
    child_proc: Optional[subprocess.Popen] = None

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal shutdown_requested
        logger.info("received signal=%s, shutting down", signum)
        shutdown_requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not shutdown_requested:
        now = _now_ist(cfg.timezone_name)
        holidays = load_holidays(cfg.holidays_file)
        if cfg.session_enabled:
            trading_day = is_trading_day_ist(now, holidays)
            market_open = is_market_open_ist(now, cfg.market_open_time, cfg.market_close_time, holidays)
        else:
            trading_day = True
            market_open = True
        reason: Optional[str] = None

        if market_open:
            if child_proc is None or child_proc.poll() is not None:
                ok, reason = _preflight_credentials()
                if ok:
                    try:
                        child_proc = _start_child(cfg)
                        reason = None
                    except Exception as exc:
                        reason = f"child_start_failed: {exc}"
                        logger.warning(reason)
            else:
                reason = None
        else:
            if child_proc is not None and child_proc.poll() is None:
                logger.info("market closed; stopping live ingestion subprocess")
            _stop_child(child_proc)
            child_proc = None
            if not trading_day:
                reason = "non_trading_day"
            else:
                reason = "outside_market_hours"

        status = "active" if (child_proc is not None and child_proc.poll() is None) else "idle"
        _write_state(
            cfg.state_file,
            _state_payload(
                cfg=cfg,
                status=status,
                child_proc=child_proc,
                market_open=market_open,
                trading_day=trading_day,
                reason=reason,
            ),
        )

        if shutdown_requested:
            break

        if market_open:
            time.sleep(max(5, int(cfg.open_retry_seconds)))
            continue

        sleep_for = int(cfg.idle_sleep_seconds)
        try:
            next_open = seconds_until_next_open_ist(now, cfg.market_open_time, holidays)
            sleep_for = max(1, min(sleep_for, int(next_open)))
        except Exception:
            sleep_for = max(1, int(cfg.idle_sleep_seconds))
        time.sleep(sleep_for)

    _stop_child(child_proc)
    return 0


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Session-aware wrapper for ingestion_app live mode.")
    parser.add_argument("--mode", choices=["live"], default="live")
    parser.add_argument("--timezone", default=None, help="IANA timezone (default from MARKET_TIMEZONE)")
    parser.add_argument("--market-open", default=None, help="HH:MM (default from MARKET_OPEN_TIME)")
    parser.add_argument("--market-close", default=None, help="HH:MM (default from MARKET_CLOSE_TIME)")
    parser.add_argument("--holidays-file", default=None, help="Path to NSE holidays json")
    parser.add_argument("--market-session-enabled", default=None, help="1/0 (default from MARKET_SESSION_ENABLED)")
    parser.add_argument("--idle-sleep-seconds", type=int, default=None)
    parser.add_argument("--open-retry-seconds", type=int, default=20)
    parser.add_argument("--state-file", default=".run/ingestion_app/session_state.json")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--max-stale-seconds", type=float, default=300.0)
    args = parser.parse_args(list(argv) if argv is not None else None)

    state_file = Path(args.state_file).resolve()
    if bool(args.healthcheck):
        return _run_healthcheck(state_file=state_file, max_stale_seconds=float(args.max_stale_seconds))

    cfg = RunnerConfig(
        mode=str(args.mode),
        session_enabled=_truthy(args.market_session_enabled if args.market_session_enabled is not None else os.getenv("MARKET_SESSION_ENABLED", "1")),
        timezone_name=str(args.timezone or os.getenv("MARKET_TIMEZONE") or "Asia/Kolkata"),
        market_open_time=str(args.market_open or os.getenv("MARKET_OPEN_TIME") or "09:15"),
        market_close_time=str(args.market_close or os.getenv("MARKET_CLOSE_TIME") or "15:30"),
        holidays_file=str(args.holidays_file or os.getenv("NSE_HOLIDAYS_FILE") or ""),
        idle_sleep_seconds=max(5, int(args.idle_sleep_seconds or os.getenv("IDLE_SLEEP_SECONDS") or 60)),
        open_retry_seconds=max(5, int(args.open_retry_seconds)),
        state_file=state_file,
        skip_validation=bool(args.skip_validation or _truthy(os.getenv("SKIP_VALIDATION", "0"))),
    )
    return _loop(cfg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    raise SystemExit(run_cli())
