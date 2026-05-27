from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Iterable, Optional

import redis

from contracts_app import configure_ist_logging, redis_connection_kwargs, resolve_namespace

from .mongo_writer import StrategyMongoWriter

logger = logging.getLogger(__name__)


def _redis_client() -> redis.Redis:
    # We use blocking XREADGROUP (block=5000ms). Ensure socket_timeout exceeds
    # the block time, otherwise redis-py raises "Timeout reading from socket".
    kwargs = dict(redis_connection_kwargs(decode_responses=True, for_pubsub=False))
    kwargs.setdefault("socket_connect_timeout", 2)
    kwargs["socket_timeout"] = max(float(kwargs.get("socket_timeout") or 0), 10.0)
    return redis.Redis(**kwargs)


def _stream_group_name() -> str:
    return str(os.getenv("SIM_PERSIST_STREAM_GROUP") or "strategy_persistence_sim").strip() or "strategy_persistence_sim"


def _consumer_name() -> str:
    return str(os.getenv("SIM_PERSIST_CONSUMER_NAME") or f"consumer-{os.getpid()}").strip() or f"consumer-{os.getpid()}"


def _ensure_group(client: redis.Redis, stream: str, group: str) -> None:
    try:
        client.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info("created stream group stream=%s group=%s", stream, group)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _decode_payload(fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = fields.get("payload")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def run_loop(*, run_id: str, health_log_interval_sec: float) -> int:
    ns = resolve_namespace("sim", run_id=run_id)
    streams = {
        ns.stream_for("votes"): ">",
        ns.stream_for("signals"): ">",
        ns.stream_for("positions"): ">",
        ns.stream_for("decision_trace"): ">",
    }

    client = _redis_client()
    group = _stream_group_name()
    consumer = _consumer_name()
    for stream in streams.keys():
        _ensure_group(client, stream, group)

    writer = StrategyMongoWriter()
    consumed = 0
    written = 0
    ignored = 0
    errors = 0
    last_message_mono: Optional[float] = None
    last_health_mono = time.monotonic()

    logger.info(
        "strategy persistence sim started run_id=%s group=%s consumer=%s streams=%s",
        run_id,
        group,
        consumer,
        list(streams.keys()),
    )

    while True:
        try:
            resp = client.xreadgroup(group, consumer, streams, count=100, block=5000)
        except KeyboardInterrupt:
            logger.info("strategy persistence sim interrupted run_id=%s", run_id)
            return 0
        except Exception as exc:
            errors += 1
            logger.warning("xreadgroup failed; retrying: %s", exc)
            time.sleep(1.0)
            continue

        now = time.monotonic()
        if not resp:
            if health_log_interval_sec > 0 and (now - last_health_mono) >= health_log_interval_sec:
                logger.info(
                    "strategy persistence sim health run_id=%s consumed=%s written=%s ignored=%s errors=%s last_message_age_s=%s",
                    run_id,
                    consumed,
                    written,
                    ignored,
                    errors,
                    round(now - last_message_mono, 1) if last_message_mono else None,
                )
                last_health_mono = now
            continue

        for stream_name, entries in resp:
            for entry_id, fields in entries:
                consumed += 1
                last_message_mono = now
                payload = _decode_payload(fields if isinstance(fields, dict) else {})
                if payload is None:
                    ignored += 1
                    try:
                        client.xack(stream_name, group, entry_id)
                    except Exception:
                        pass
                    continue
                try:
                    ok = writer.write_strategy_event(payload)
                    written += 1 if ok else 0
                    ignored += 0 if ok else 1
                except Exception as exc:
                    errors += 1
                    logger.warning("mongo write failed: %s", exc)
                finally:
                    try:
                        client.xack(stream_name, group, entry_id)
                    except Exception:
                        pass


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Strategy persistence for SIM streams -> *_sim mongo collections")
    parser.add_argument("--run-id", default=str(os.getenv("SIM_RUN_ID") or "").strip())
    parser.add_argument("--health-log-interval-sec", type=float, default=30.0)
    args = parser.parse_args(raw_argv)

    run_id = str(args.run_id or "").strip()
    if not run_id:
        raise SystemExit("SIM_RUN_ID/--run-id is required")
    return run_loop(run_id=run_id, health_log_interval_sec=max(0.0, float(args.health_log_interval_sec)))


if __name__ == "__main__":
    configure_ist_logging(level=logging.INFO)
    raise SystemExit(run_cli())

