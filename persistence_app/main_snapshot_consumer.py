from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import redis

from contracts_app import configure_ist_logging, find_matching_python_processes, isoformat_ist, redis_connection_kwargs, snapshot_topic

from .health import evaluate as evaluate_health
from .mongo_writer import SnapshotMongoWriter


logger = logging.getLogger(__name__)
MIN_PAPER_DAYS = 10
MIN_SHADOW_DAYS = 10
MAX_CAPPED_LIVE_SIZE_MULTIPLIER = 0.25


def _ist_now_iso() -> str:
    return isoformat_ist()


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


def _launch_detached(*, cmd: list[str], run_dir: str) -> dict:
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
        "component": "persistence_app",
        "pid": int(proc.pid),
        "command": cmd,
        "started_at_ist": _ist_now_iso(),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _redis_client() -> redis.Redis:
    return redis.Redis(**redis_connection_kwargs(decode_responses=True, for_pubsub=True))


def _validate_rollout_context(
    *,
    rollout_stage: str,
    paper_days_observed: int,
    shadow_days_observed: int,
    position_size_multiplier: float,
    ml_runtime_enabled: bool,
    offline_strict_positive_passed: bool,
    approved_for_runtime: bool,
) -> Optional[str]:
    stage = str(rollout_stage or "").strip().lower()
    if stage in {"shadow", "capped_live"} and int(paper_days_observed) < MIN_PAPER_DAYS:
        return f"rollout requires >= {MIN_PAPER_DAYS} paper days before {stage}"
    if stage == "capped_live" and int(shadow_days_observed) < MIN_SHADOW_DAYS:
        return f"capped_live requires >= {MIN_SHADOW_DAYS} shadow days"
    if stage == "capped_live" and float(position_size_multiplier) > MAX_CAPPED_LIVE_SIZE_MULTIPLIER:
        return f"capped_live position_size_multiplier must be <= {MAX_CAPPED_LIVE_SIZE_MULTIPLIER}"
    if bool(ml_runtime_enabled):
        if stage != "capped_live":
            return "ml runtime is allowed only in capped_live stage"
        if not bool(offline_strict_positive_passed):
            return "ml runtime requires offline_strict_positive_passed=true"
        if not bool(approved_for_runtime):
            return "ml runtime requires approved_for_runtime=true"
    return None


def run_loop(*, topic: str, health_log_interval_sec: float, rollout_context: Optional[dict[str, object]] = None) -> int:
    writer = SnapshotMongoWriter()
    client = _redis_client()
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(topic)
    logger.info("persistence_app subscribed topic=%s", topic)
    if isinstance(rollout_context, dict) and rollout_context:
        logger.info("persistence_app rollout_context=%s", json.dumps(rollout_context, ensure_ascii=False, default=str))
    consumed_count = 0
    written_count = 0
    ignored_count = 0
    error_count = 0
    last_message_monotonic: Optional[float] = None
    last_health_log_monotonic = time.monotonic()

    try:
        while True:
            try:
                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not msg:
                    now_mono = time.monotonic()
                    if health_log_interval_sec > 0 and (now_mono - last_health_log_monotonic) >= health_log_interval_sec:
                        last_age = None
                        if last_message_monotonic is not None:
                            last_age = round(now_mono - last_message_monotonic, 3)
                        logger.info(
                            "persistence_app health consumed=%s written=%s ignored=%s errors=%s seconds_since_last_message=%s",
                            consumed_count,
                            written_count,
                            ignored_count,
                            error_count,
                            last_age,
                        )
                        last_health_log_monotonic = now_mono
                    continue
                data = msg.get("data")
                if not isinstance(data, str):
                    continue
                consumed_count += 1
                last_message_monotonic = time.monotonic()
                payload = json.loads(data)
                ok = writer.write_snapshot_event(payload)
                if not ok:
                    ignored_count += 1
                    logger.debug("ignored non-snapshot payload")
                else:
                    written_count += 1
            except Exception as exc:
                error_count += 1
                logger.warning("persistence_app message handling error: %s", exc)
            time.sleep(0.001)
    except KeyboardInterrupt:
        logger.info("persistence_app interrupted")
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
    return 0


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Top-level persistence process for snapshot events")
    parser.add_argument("--event-topic", default=None)
    parser.add_argument("--health-log-interval-sec", type=float, default=30.0)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--run-dir", default=".run/persistence_app")
    parser.add_argument("--health-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--health-max-age-seconds", type=float, default=900.0)
    parser.add_argument("--rollout-stage", choices=["paper", "shadow", "capped_live"], default="paper")
    parser.add_argument("--paper-days-observed", type=int, default=0)
    parser.add_argument("--shadow-days-observed", type=int, default=0)
    parser.add_argument("--position-size-multiplier", type=float, default=1.0)
    parser.add_argument("--halt-consecutive-losses", type=int, default=3)
    parser.add_argument("--halt-daily-dd-pct", type=float, default=-0.75)
    parser.add_argument("--ml-runtime-enabled", action="store_true")
    parser.add_argument("--offline-strict-positive-passed", action="store_true")
    parser.add_argument("--approved-for-runtime", action="store_true")
    args = parser.parse_args(raw_argv)

    controls = {
        "stop_command": "python -m persistence_app.stop",
        "health_command": f"python -m persistence_app.health --max-age-seconds {max(1.0, float(args.health_max_age_seconds))}",
        "logs_dir": str(Path(args.run_dir).resolve()),
    }

    if not bool(args.foreground):
        self_pid = int(os.getpid())
        running = [
            (pid, cmdline)
            for pid, cmdline in find_matching_python_processes(["persistence_app.main_snapshot_consumer --foreground", "persistence_app.main_snapshot_consumer"])
            if int(pid) != self_pid
        ]
        if running:
            pids = [int(pid) for pid, _ in running[:20]]
            result, code = evaluate_health(max_age_seconds=max(1.0, float(args.health_max_age_seconds)))
            result["launcher"] = {
                "component": "persistence_app",
                "action": "already_running",
                "pids": pids,
                "duplicate_processes_detected": len(pids) > 1,
                "run_dir": str(Path(args.run_dir).resolve()),
            }
            result["controls"] = controls
            print(json.dumps(result, ensure_ascii=False, default=str))
            return int(code)

        launch_cmd = [sys.executable, "-m", "persistence_app.main_snapshot_consumer", *raw_argv, "--foreground"]
        launch_meta = _launch_detached(cmd=launch_cmd, run_dir=str(args.run_dir))
        deadline = time.monotonic() + max(1.0, float(args.health_timeout_seconds))
        result = None
        code = 2
        while time.monotonic() < deadline:
            result, code = evaluate_health(max_age_seconds=max(1.0, float(args.health_max_age_seconds)))
            if code in (0, 1):
                break
            time.sleep(1.0)
        if result is None:
            result, code = evaluate_health(max_age_seconds=max(1.0, float(args.health_max_age_seconds)))
        result["launcher"] = launch_meta
        result["controls"] = controls
        print(json.dumps(result, ensure_ascii=False, default=str))
        return int(code)

    topic = str(args.event_topic or snapshot_topic()).strip() or snapshot_topic()
    rollout_error = _validate_rollout_context(
        rollout_stage=str(args.rollout_stage),
        paper_days_observed=int(args.paper_days_observed),
        shadow_days_observed=int(args.shadow_days_observed),
        position_size_multiplier=float(args.position_size_multiplier),
        ml_runtime_enabled=bool(args.ml_runtime_enabled),
        offline_strict_positive_passed=bool(args.offline_strict_positive_passed),
        approved_for_runtime=bool(args.approved_for_runtime),
    )
    if rollout_error:
        raise SystemExit(rollout_error)
    rollout_context = {
        "rollout_stage": str(args.rollout_stage),
        "paper_days_observed": int(args.paper_days_observed),
        "shadow_days_observed": int(args.shadow_days_observed),
        "position_size_multiplier": float(args.position_size_multiplier),
        "halt_consecutive_losses": int(args.halt_consecutive_losses),
        "halt_daily_dd_pct": float(args.halt_daily_dd_pct),
        "ml_runtime_enabled": bool(args.ml_runtime_enabled),
        "offline_strict_positive_passed": bool(args.offline_strict_positive_passed),
        "approved_for_runtime": bool(args.approved_for_runtime),
    }
    return run_loop(
        topic=topic,
        health_log_interval_sec=max(0.0, float(args.health_log_interval_sec)),
        rollout_context=rollout_context,
    )


if __name__ == "__main__":
    configure_ist_logging(level=logging.INFO)
    raise SystemExit(run_cli())
