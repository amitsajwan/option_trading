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

from contracts_app import (
    configure_ist_logging,
    find_matching_python_processes,
    isoformat_ist,
    redis_connection_kwargs,
    strategy_decision_trace_topic,
    strategy_position_topic,
    strategy_vote_topic,
    trade_signal_topic,
)

from .mongo_writer import StrategyMongoWriter

logger = logging.getLogger(__name__)
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
        "component": "persistence_app_strategy",
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


def run_loop(*, topics: list[str], health_log_interval_sec: float) -> int:
    writer = StrategyMongoWriter()
    client = _redis_client()
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(*topics)
    logger.info("strategy persistence subscribed topics=%s", topics)
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
                            "strategy persistence health consumed=%s written=%s ignored=%s errors=%s seconds_since_last_message=%s",
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
                ok = writer.write_strategy_event(payload)
                if not ok:
                    ignored_count += 1
                else:
                    written_count += 1
            except Exception as exc:
                error_count += 1
                logger.warning("strategy persistence message handling error: %s", exc)
            time.sleep(0.001)
    except KeyboardInterrupt:
        logger.info("strategy persistence interrupted")
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
    return 0


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Top-level persistence process for strategy events")
    parser.add_argument("--vote-topic", default=None)
    parser.add_argument("--signal-topic", default=None)
    parser.add_argument("--position-topic", default=None)
    parser.add_argument("--trace-topic", default=None)
    parser.add_argument("--health-log-interval-sec", type=float, default=30.0)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--run-dir", default=".run/persistence_app_strategy")
    args = parser.parse_args(raw_argv)

    if not bool(args.foreground):
        self_pid = int(os.getpid())
        running = [
            (pid, cmdline)
            for pid, cmdline in find_matching_python_processes(["persistence_app.main_strategy_consumer --foreground", "persistence_app.main_strategy_consumer"])
            if int(pid) != self_pid
        ]
        if running:
            print(
                json.dumps(
                    {
                        "component": "persistence_app_strategy",
                        "action": "already_running",
                        "pids": [int(pid) for pid, _ in running[:20]],
                        "run_dir": str(Path(args.run_dir).resolve()),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        launch_cmd = [sys.executable, "-m", "persistence_app.main_strategy_consumer", *raw_argv, "--foreground"]
        launch_meta = _launch_detached(cmd=launch_cmd, run_dir=str(args.run_dir))
        print(json.dumps(launch_meta, ensure_ascii=False, default=str))
        return 0

    topics = [
        str(args.vote_topic or strategy_vote_topic()).strip() or strategy_vote_topic(),
        str(args.signal_topic or trade_signal_topic()).strip() or trade_signal_topic(),
        str(args.position_topic or strategy_position_topic()).strip() or strategy_position_topic(),
        str(args.trace_topic or strategy_decision_trace_topic()).strip() or strategy_decision_trace_topic(),
    ]
    return run_loop(topics=topics, health_log_interval_sec=max(0.0, float(args.health_log_interval_sec)))


if __name__ == "__main__":
    configure_ist_logging(level=logging.INFO)
    raise SystemExit(run_cli())
