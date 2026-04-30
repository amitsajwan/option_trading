from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

from contracts_app import (
    build_snapshot_event,
    configure_ist_logging,
    find_matching_python_processes,
    isoformat_ist,
    is_market_open_ist,
    is_trading_day_ist,
    load_holidays,
    seconds_until_next_open_ist,
    snapshot_topic,
)
from snapshot_app.core.market_snapshot import LiveMarketSnapshotBuilder
from snapshot_app.core.market_snapshot_contract import CONTRACT_ID as MARKET_SNAPSHOT_CONTRACT_ID, validate_market_snapshot

from .health import evaluate as evaluate_health
from .publisher import EventPublisher
from .redis_publisher import RedisEventPublisher


logger = logging.getLogger(__name__)
DEFAULT_MARKET_TZ = "Asia/Kolkata"
DEFAULT_MARKET_OPEN = "09:15"
DEFAULT_MARKET_CLOSE = "15:30"
DEFAULT_IDLE_SLEEP_SECONDS = 60
IST = timezone(timedelta(hours=5, minutes=30))


def _resolve_instrument(value: Optional[str]) -> str:
    if value and str(value).strip():
        return str(value).strip().upper()
    for key in ("INSTRUMENT_SYMBOL", "INSTRUMENT_KEY", "INSTRUMENT_TRADING_SYMBOL"):
        raw = str(os.getenv(key) or "").strip()
        if raw:
            return raw.upper()
    return "BANKNIFTY26MARFUT"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _zone_or_default(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return IST


def _market_api_healthy(*, base_url: str, timeout_seconds: float) -> bool:
    url = f"{str(base_url).rstrip('/')}/health"
    req = Request(url=url, method="GET")
    try:
        with urlopen(req, timeout=max(0.5, float(timeout_seconds))) as response:
            code = int(getattr(response, "status", 200))
            return 200 <= code < 300
    except URLError:
        return False
    except Exception:
        return False


def _append_jsonl(path: Optional[str], payload: dict[str, Any]) -> None:
    if not path:
        return
    out = Path(path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _ist_now_iso() -> str:
    return isoformat_ist()


def _build_run_id() -> str:
    stamp = datetime.now(tz=_zone_or_default(DEFAULT_MARKET_TZ)).strftime("%Y%m%d_%H%M%S")
    return f"live_{stamp}_{uuid4().hex[:8]}"


def _detached_popen_kwargs() -> dict:
    if os.name == "nt":
        detached_process = 0x00000008
        create_new_process_group = 0x00000200
        create_no_window = 0x08000000
        return {
            "creationflags": detached_process | create_new_process_group | create_no_window,
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


def _launch_detached(*, cmd: list[str], run_dir: str) -> dict[str, Any]:
    out_dir = Path(run_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "stdout.log"
    stderr_path = out_dir / "stderr.log"
    meta_path = out_dir / "process.json"
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        proc = subprocess.Popen(
            cmd,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            **_detached_popen_kwargs(),
        )
    meta = {
        "component": "snapshot_app",
        "pid": int(proc.pid),
        "command": cmd,
        "started_at_ist": _ist_now_iso(),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def run_loop(
    *,
    instrument: str,
    market_api_base: str,
    dashboard_api_base: str,
    timeout_seconds: float,
    poll_interval_sec: float,
    ohlc_limit: int,
    publisher: EventPublisher,
    event_topic: str,
    out_jsonl: Optional[str],
    health_log_interval_sec: float,
    market_session_enabled: bool,
    market_timezone: str,
    market_open_time: str,
    market_close_time: str,
    holidays_file: Optional[str],
    idle_sleep_seconds: int,
    build_run_id: str,
    parquet_root: Optional[str] = None,
) -> int:
    builder = LiveMarketSnapshotBuilder(
        instrument=instrument,
        market_api_base=market_api_base,
        dashboard_api_base=dashboard_api_base,
        timeout_seconds=timeout_seconds,
        parquet_root=parquet_root,
    )
    if parquet_root:
        logger.info("snapshot_app velocity context parquet_root=%s", parquet_root)
    else:
        logger.warning(
            "snapshot_app velocity context parquet_root not configured; "
            "ctx_gap_*, ctx_am_vol_vs_yday, vol_spike_ratio will be NaN in live snapshots"
        )
    last_snapshot_id: Optional[str] = None

    logger.info("snapshot_app started instrument=%s topic=%s", instrument, event_topic)
    published_count = 0
    error_count = 0
    last_published_monotonic: Optional[float] = None
    last_health_log_monotonic = time.monotonic()
    while True:
        if market_session_enabled:
            now_gate = datetime.now(tz=_zone_or_default(market_timezone))
            holidays = load_holidays(holidays_file)
            if not is_market_open_ist(now_gate, market_open_time, market_close_time, holidays):
                reason = "non_trading_day" if not is_trading_day_ist(now_gate, holidays) else "outside_market_hours"
                if health_log_interval_sec > 0:
                    now_mono = time.monotonic()
                    if (now_mono - last_health_log_monotonic) >= health_log_interval_sec:
                        logger.info(
                            "snapshot_app idle reason=%s now=%s next_open_in_sec=%s",
                            reason,
                            now_gate.isoformat(),
                            seconds_until_next_open_ist(now_gate, market_open_time, holidays),
                        )
                        last_health_log_monotonic = now_mono
                sleep_for = max(1, int(idle_sleep_seconds))
                try:
                    next_open = seconds_until_next_open_ist(now_gate, market_open_time, holidays)
                    sleep_for = max(1, min(sleep_for, int(next_open)))
                except Exception:
                    sleep_for = max(1, int(idle_sleep_seconds))
                time.sleep(sleep_for)
                continue

        if not _market_api_healthy(base_url=market_api_base, timeout_seconds=min(2.0, float(timeout_seconds))):
            if health_log_interval_sec > 0:
                now_mono = time.monotonic()
                if (now_mono - last_health_log_monotonic) >= health_log_interval_sec:
                    logger.info("snapshot_app idle reason=market_api_unreachable base=%s", market_api_base)
                    last_health_log_monotonic = now_mono
            time.sleep(max(5, min(int(idle_sleep_seconds), 30)))
            continue
        try:
            snapshot = builder.build_snapshot(ohlc_limit=int(ohlc_limit))
            validate_market_snapshot(snapshot, raise_on_error=True)
            snapshot_id = str(snapshot.get("snapshot_id") or "")
            if not snapshot_id or snapshot_id == last_snapshot_id:
                time.sleep(poll_interval_sec)
                continue
            last_snapshot_id = snapshot_id

            event = build_snapshot_event(
                snapshot=snapshot,
                source="snapshot_app",
                metadata={
                    "session_timezone": "IST",
                    "snapshot_contract_id": MARKET_SNAPSHOT_CONTRACT_ID,
                    "build_run_id": build_run_id,
                },
            )
            publisher.publish(topic=event_topic, payload=event)
            _append_jsonl(out_jsonl, event)
            published_count += 1
            last_published_monotonic = time.monotonic()
        except KeyboardInterrupt:
            logger.info("snapshot_app interrupted")
            break
        except Exception as exc:
            error_count += 1
            logger.warning("snapshot_app loop error: %s", exc)
        now_mono = time.monotonic()
        if health_log_interval_sec > 0 and (now_mono - last_health_log_monotonic) >= health_log_interval_sec:
            last_age = None
            if last_published_monotonic is not None:
                last_age = round(now_mono - last_published_monotonic, 3)
            logger.info(
                "snapshot_app health published=%s errors=%s last_snapshot_id=%s seconds_since_last_publish=%s build_run_id=%s",
                published_count,
                error_count,
                last_snapshot_id,
                last_age,
                build_run_id,
            )
            last_health_log_monotonic = now_mono
        time.sleep(poll_interval_sec)
    return 0


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Top-level MarketSnapshot live producer")
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--market-api-base", default="http://127.0.0.1:8004")
    parser.add_argument("--dashboard-api-base", default="http://127.0.0.1:8002")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--poll-interval-sec", type=float, default=1.0)
    parser.add_argument("--ohlc-limit", type=int, default=1800)
    parser.add_argument("--out-jsonl", default=".run/snapshot_app/events.jsonl")
    parser.add_argument("--event-topic", default=None)
    parser.add_argument("--health-log-interval-sec", type=float, default=30.0)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--run-dir", default=".run/snapshot_app")
    parser.add_argument("--health-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--health-max-age-seconds", type=float, default=900.0)
    args = parser.parse_args(raw_argv)

    controls = {
        "stop_command": "python -m snapshot_app.stop",
        "health_command": (
            f"python -m snapshot_app.health --events-path {str(args.out_jsonl)} "
            f"--max-age-seconds {max(1.0, float(args.health_max_age_seconds))}"
        ),
        "logs_dir": str(Path(args.run_dir).resolve()),
    }

    if not bool(args.foreground):
        self_pid = int(os.getpid())
        running = [
            (pid, cmdline)
            for pid, cmdline in find_matching_python_processes(["snapshot_app.main_live --foreground", "snapshot_app.main_live --instrument"])
            if int(pid) != self_pid
        ]
        if running:
            pids = [int(pid) for pid, _ in running[:20]]
            result, code = evaluate_health(
                events_path=str(args.out_jsonl),
                max_age_seconds=max(1.0, float(args.health_max_age_seconds)),
            )
            result["launcher"] = {
                "component": "snapshot_app",
                "action": "already_running",
                "pids": pids,
                "duplicate_processes_detected": len(pids) > 1,
                "run_dir": str(Path(args.run_dir).resolve()),
            }
            result["controls"] = controls
            print(json.dumps(result, ensure_ascii=False, default=str))
            return int(code)

        launch_cmd = [sys.executable, "-m", "snapshot_app.main_live", *raw_argv, "--foreground"]
        launch_meta = _launch_detached(cmd=launch_cmd, run_dir=str(args.run_dir))
        deadline = time.monotonic() + max(1.0, float(args.health_timeout_seconds))
        result = None
        code = 2
        while time.monotonic() < deadline:
            result, code = evaluate_health(
                events_path=str(args.out_jsonl),
                max_age_seconds=max(1.0, float(args.health_max_age_seconds)),
            )
            if code in (0, 1):
                break
            time.sleep(1.0)
        if result is None:
            result, code = evaluate_health(
                events_path=str(args.out_jsonl),
                max_age_seconds=max(1.0, float(args.health_max_age_seconds)),
            )
        result["launcher"] = launch_meta
        result["controls"] = controls
        print(json.dumps(result, ensure_ascii=False, default=str))
        return int(code)

    instrument = _resolve_instrument(args.instrument)
    topic = str(args.event_topic or snapshot_topic()).strip() or snapshot_topic()
    market_session_enabled = _truthy(os.getenv("MARKET_SESSION_ENABLED", "0"))
    market_timezone = str(os.getenv("MARKET_TIMEZONE") or DEFAULT_MARKET_TZ).strip() or DEFAULT_MARKET_TZ
    market_open_time = str(os.getenv("MARKET_OPEN_TIME") or DEFAULT_MARKET_OPEN).strip() or DEFAULT_MARKET_OPEN
    market_close_time = str(os.getenv("MARKET_CLOSE_TIME") or DEFAULT_MARKET_CLOSE).strip() or DEFAULT_MARKET_CLOSE
    holidays_file = str(os.getenv("NSE_HOLIDAYS_FILE") or "").strip() or None
    idle_sleep_seconds = max(5, int(os.getenv("IDLE_SLEEP_SECONDS") or DEFAULT_IDLE_SLEEP_SECONDS))
    publisher = RedisEventPublisher()
    build_run_id = str(os.getenv("SNAPSHOT_BUILD_RUN_ID") or "").strip() or _build_run_id()
    # Velocity feature context: training uses snapshots_ml_flat_v2 under this root.
    # When present, LiveVelocityAccumulator reads prev_day_close / midday option
    # volume to populate ctx_gap_* and vol_spike_ratio consistently with training.
    parquet_root_raw = str(os.getenv("SNAPSHOT_PARQUET_ROOT") or "").strip()
    parquet_root: Optional[str] = None
    if parquet_root_raw and Path(parquet_root_raw).exists():
        parquet_root = parquet_root_raw
    elif parquet_root_raw:
        logger.warning(
            "SNAPSHOT_PARQUET_ROOT=%s does not exist; velocity context will be NaN",
            parquet_root_raw,
        )
    return run_loop(
        instrument=instrument,
        market_api_base=str(args.market_api_base),
        dashboard_api_base=str(args.dashboard_api_base),
        timeout_seconds=float(args.timeout_seconds),
        poll_interval_sec=max(0.1, float(args.poll_interval_sec)),
        ohlc_limit=max(60, int(args.ohlc_limit)),
        publisher=publisher,
        event_topic=topic,
        out_jsonl=(str(args.out_jsonl).strip() or None),
        health_log_interval_sec=max(0.0, float(args.health_log_interval_sec)),
        market_session_enabled=market_session_enabled,
        market_timezone=market_timezone,
        market_open_time=market_open_time,
        market_close_time=market_close_time,
        holidays_file=holidays_file,
        idle_sleep_seconds=idle_sleep_seconds,
        build_run_id=build_run_id,
        parquet_root=parquet_root,
    )


if __name__ == "__main__":
    configure_ist_logging(level=logging.INFO)
    raise SystemExit(run_cli())
