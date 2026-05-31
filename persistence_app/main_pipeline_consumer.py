"""Pipeline decision event persistence — SIM streams → pipeline_decision_events MongoDB collection.

Consumes the 7 new decision streams (regime→execution) and writes one document
per event to a single flat collection.  The trace_id field threads all 7 stage
events together so the dashboard can reconstruct a full decision chain with a
single query.

Usage:
    python -m persistence_app.main_pipeline_consumer --run-id <run_id>
    SIM_RUN_ID=<run_id> python -m persistence_app.main_pipeline_consumer
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import redis

from contracts_app import configure_ist_logging, redis_connection_kwargs, resolve_namespace

logger = logging.getLogger(__name__)

# slug → stage label (slug matches Namespace.stream_for() slugs)
_SLUG_TO_STAGE: dict[str, str] = {
    "regime_decisions":    "regime",
    "entry_decisions":     "entry",
    "direction_decisions": "direction",
    "depth_decisions":     "depth",
    "strike_decisions":    "strike",
    "risk_decisions":      "risk",
    "execution_events":    "execution",
}

_STAGE_SLUGS = list(_SLUG_TO_STAGE.keys())

_COLLECTION = "pipeline_decision_events"


# ---------------------------------------------------------------------------
# Redis helpers (same pattern as main_strategy_stream_consumer.py)
# ---------------------------------------------------------------------------

def _redis_client() -> redis.Redis:
    kwargs = dict(redis_connection_kwargs(decode_responses=True, for_pubsub=False))
    kwargs.setdefault("socket_connect_timeout", 2)
    kwargs["socket_timeout"] = max(float(kwargs.get("socket_timeout") or 0), 10.0)
    return redis.Redis(**kwargs)


def _ensure_group(client: redis.Redis, stream: str, group: str) -> None:
    try:
        client.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info("created consumer group stream=%s group=%s", stream, group)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _decode_payload(fields: Any) -> Optional[dict[str, Any]]:
    raw = (fields or {}).get("payload")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _stage_from_stream(stream_name: str) -> str:
    """Extract stage label from stream name. Sim streams are stream:{slug}:sim:{run_id}."""
    parts = stream_name.split(":")
    slug = parts[1] if len(parts) >= 2 else stream_name
    return _SLUG_TO_STAGE.get(slug, slug)


# ---------------------------------------------------------------------------
# Document builder
# ---------------------------------------------------------------------------

def _outcome(stage: str, p: dict) -> str:
    """Compact human-readable outcome string for quick scanning."""
    try:
        if stage == "regime":
            return f"{p.get('regime','?')} {float(p.get('confidence') or 0):.2f}"
        if stage == "entry":
            return "allowed" if p.get("allowed") else "blocked"
        if stage == "direction":
            return "vetoed" if p.get("vetoed") else str(p.get("direction") or "?")
        if stage == "depth":
            if not p.get("proceed"):
                return "blocked"
            return f"{'aligned' if p.get('depth_aligned') else 'pass'} {float(p.get('confidence') or 0):.2f}"
        if stage == "strike":
            return "skipped" if p.get("skipped") else str(p.get("strike") or "?")
        if stage == "risk":
            return f"approved {p.get('approved_lots',0)}L" if p.get("approved") else "rejected"
        if stage == "execution":
            return str(p.get("signal_type") or "SKIP")
    except Exception:
        pass
    return ""


def _build_doc(stage: str, payload: dict, received_at: datetime) -> dict:
    return {
        "trace_id":       str(payload.get("trace_id") or ""),
        "run_id":         str(payload.get("run_id") or ""),
        "stage":          stage,
        "event_id":       str(payload.get("event_id") or ""),
        "parent_event_id":str(payload.get("parent_event_id") or ""),
        "event_type":     str(payload.get("event_type") or ""),
        "confidence":     payload.get("confidence"),
        "outcome":        _outcome(stage, payload),
        "plugin_id":      str(payload.get("plugin_id") or ""),
        "plugin_version": str(payload.get("plugin_version") or ""),
        "parity_mode":    str(payload.get("parity_mode") or ""),
        "timestamp":      str(payload.get("timestamp") or ""),
        "_received_at":   received_at,
        "payload":        payload,
    }


# ---------------------------------------------------------------------------
# MongoDB connection (same env vars as mongo_writer.py)
# ---------------------------------------------------------------------------

def _get_collection():
    from pymongo import ASCENDING, DESCENDING, MongoClient
    uri  = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    sel_ms = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS") or "5000")
    if uri:
        client = MongoClient(uri, serverSelectionTimeoutMS=sel_ms)
    else:
        client = MongoClient(
            host=str(os.getenv("MONGO_HOST") or "localhost"),
            port=int(os.getenv("MONGO_PORT") or "27017"),
            serverSelectionTimeoutMS=sel_ms,
        )
    coll = client[name][_COLLECTION]
    # Indexes for the two main query patterns
    coll.create_index([("trace_id", ASCENDING), ("_received_at", ASCENDING)], background=True)
    coll.create_index([("run_id", ASCENDING), ("_received_at", DESCENDING)], background=True)
    logger.info("pipeline collection ready db=%s collection=%s", name, _COLLECTION)
    return coll


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_loop(*, run_id: str, health_log_interval_sec: float = 30.0) -> int:
    ns     = resolve_namespace("sim", run_id=run_id)
    streams = {ns.stream_for(slug): ">" for slug in _STAGE_SLUGS}

    client   = _redis_client()
    group    = str(os.getenv("PIPELINE_PERSIST_GROUP")    or "pipeline_persistence_sim")
    consumer = str(os.getenv("PIPELINE_PERSIST_CONSUMER") or f"consumer-{os.getpid()}")

    for stream in streams:
        _ensure_group(client, stream, group)

    coll = _get_collection()
    consumed = written = errors = 0
    last_health = time.monotonic()

    logger.info("pipeline persistence started run_id=%s group=%s consumer=%s streams=%d",
                run_id, group, consumer, len(streams))

    while True:
        try:
            resp = client.xreadgroup(group, consumer, streams, count=100, block=5000)
        except KeyboardInterrupt:
            logger.info("pipeline persistence interrupted")
            return 0
        except Exception as exc:
            errors += 1
            logger.warning("xreadgroup error: %s", exc)
            time.sleep(1.0)
            continue

        now = time.monotonic()
        if not resp:
            if health_log_interval_sec > 0 and (now - last_health) >= health_log_interval_sec:
                logger.info("pipeline persistence health consumed=%d written=%d errors=%d",
                            consumed, written, errors)
                last_health = now
            continue

        received_at = datetime.now(timezone.utc)
        docs: list[dict] = []
        ack_map: dict[str, list[str]] = {}

        for stream_name, entries in resp:
            stage = _stage_from_stream(str(stream_name))
            for entry_id, fields in entries:
                consumed += 1
                payload = _decode_payload(fields)
                if payload and payload.get("trace_id"):
                    docs.append(_build_doc(stage, payload, received_at))
                ack_map.setdefault(str(stream_name), []).append(str(entry_id))

        if docs:
            try:
                coll.insert_many(docs, ordered=False)
                written += len(docs)
            except Exception as exc:
                errors += 1
                logger.warning("mongo insert failed: %s", exc)

        for sname, ids in ack_map.items():
            try:
                client.xack(sname, group, *ids)
            except Exception:
                pass


def run_cli(argv=None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(description="Persist pipeline decision events (sim→mongodb)")
    p.add_argument("--run-id", default=str(os.getenv("SIM_RUN_ID") or "").strip())
    p.add_argument("--health-log-interval-sec", type=float, default=30.0)
    args = p.parse_args(raw)
    if not args.run_id.strip():
        raise SystemExit("SIM_RUN_ID / --run-id required")
    return run_loop(run_id=args.run_id.strip(), health_log_interval_sec=args.health_log_interval_sec)


if __name__ == "__main__":
    configure_ist_logging(level=logging.INFO)
    raise SystemExit(run_cli())
