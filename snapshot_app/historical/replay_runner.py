"""Replay prebuilt historical snapshots to redis topic."""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import redis

from contracts_app import build_snapshot_event, historical_snapshot_topic, redis_connection_kwargs
from snapshot_app.core.market_snapshot_contract import validate_market_snapshot
from snapshot_app.redis_publisher import RedisEventPublisher

from .parquet_store import ParquetStore
from .snapshot_access import (
    DEFAULT_HISTORICAL_PARQUET_BASE,
    SNAPSHOT_DATASET_CANONICAL,
    SNAPSHOT_INPUT_MODE_CANONICAL,
    require_snapshot_access,
)

logger = logging.getLogger(__name__)


DEFAULT_PARQUET_BASE = DEFAULT_HISTORICAL_PARQUET_BASE
REPLAY_STATUS_KEY = "system:historical:replay_status"
HISTORICAL_READY_KEY = "system:historical:data_ready"
VIRTUAL_TIME_ENABLED_KEY = "system:virtual_time:enabled"
VIRTUAL_TIME_CURRENT_KEY = "system:virtual_time:current"


def _redis_client() -> redis.Redis:
    return redis.Redis(**redis_connection_kwargs(decode_responses=True))


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _snapshot_timestamp(snapshot: dict) -> Optional[str]:
    session = snapshot.get("session_context") if isinstance(snapshot.get("session_context"), dict) else {}
    for key in ("timestamp",):
        text = str(session.get(key) or snapshot.get(key) or "").strip()
        if text:
            return text
    return None


def _snapshot_trade_date(snapshot: dict) -> Optional[str]:
    session = snapshot.get("session_context") if isinstance(snapshot.get("session_context"), dict) else {}
    for raw in (session.get("date"), snapshot.get("trade_date"), _snapshot_timestamp(snapshot)):
        text = str(raw or "").strip()
        if len(text) >= 10:
            return text[:10]
    return None


def _write_replay_status(client: redis.Redis, payload: dict) -> None:
    client.set(REPLAY_STATUS_KEY, json.dumps(payload, ensure_ascii=False, default=str))
    client.set(HISTORICAL_READY_KEY, "1" if payload.get("data_ready") else "0")
    client.set(VIRTUAL_TIME_ENABLED_KEY, "1" if payload.get("virtual_time_enabled") else "0")
    if payload.get("virtual_time_current"):
        client.set(VIRTUAL_TIME_CURRENT_KEY, str(payload.get("virtual_time_current")))
    else:
        client.delete(VIRTUAL_TIME_CURRENT_KEY)


def _load_snapshots(
    *,
    store: ParquetStore,
    start_date: Optional[str],
    end_date: Optional[str],
) -> pd.DataFrame:
    snapshot_days = store.available_snapshot_days()
    if not snapshot_days:
        return pd.DataFrame()

    start = start_date or snapshot_days[0]
    end = end_date or snapshot_days[-1]
    frame = store.snapshots_for_date_range(start, end)
    if len(frame) == 0:
        return frame
    frame["timestamp"] = pd.to_datetime(frame.get("timestamp"), errors="coerce")
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    return frame


def _snapshot_from_row(row: pd.Series) -> Optional[dict]:
    raw = row.get("snapshot_raw_json")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
    return None


def replay_snapshots(
    *,
    parquet_base: str | Path,
    topic: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    speed: float = 0.0,
    loop: bool = False,
    emit_jsonl: Optional[str] = None,
    max_events: int = 0,
) -> dict:
    """Replay snapshots from parquet to redis topic."""
    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_CANONICAL,
        context="historical.replay_runner",
        parquet_base=Path(parquet_base),
        min_day=start_date,
        max_day=end_date,
    )
    store = ParquetStore(parquet_base, snapshots_dataset=SNAPSHOT_DATASET_CANONICAL)
    publisher = RedisEventPublisher()
    out_path = Path(emit_jsonl).resolve() if emit_jsonl else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    resolved_topic = str(topic or historical_snapshot_topic()).strip()
    if not resolved_topic:
        resolved_topic = historical_snapshot_topic()

    events_limit = max(0, int(max_events))
    emitted = 0
    cycles = 0
    started_at = time.time()
    sleep_sec = 0.0 if float(speed) <= 0 else (60.0 / float(speed))
    status_client = _redis_client()
    base_status = {
        "status": "running",
        "topic": resolved_topic,
        "start_date": start_date,
        "end_date": end_date,
        "speed": float(speed),
        "loop": bool(loop),
        "max_events": events_limit,
        "events_emitted": 0,
        "cycles": 0,
        "current_replay_timestamp": None,
        "current_trade_date": start_date,
        "started_at": _now_iso(),
        "finished_at": None,
        "data_ready": False,
        "virtual_time_enabled": False,
        "virtual_time_current": None,
    }
    _write_replay_status(status_client, base_status)

    try:
        while True:
            frame = _load_snapshots(store=store, start_date=start_date, end_date=end_date)
            if len(frame) == 0:
                result = {
                    "status": "no_snapshots",
                    "topic": resolved_topic,
                    "start_date": start_date,
                    "end_date": end_date,
                    "events_emitted": emitted,
                    **snapshot_access.to_metadata(),
                }
                _write_replay_status(
                    status_client,
                    {
                        **base_status,
                        "status": "no_snapshots",
                        "events_emitted": emitted,
                        "cycles": cycles,
                        "finished_at": _now_iso(),
                    },
                )
                return result

            cycles += 1
            for _, row in frame.iterrows():
                if events_limit > 0 and emitted >= events_limit:
                    elapsed = round(time.time() - started_at, 2)
                    result = {
                        "status": "complete",
                        "topic": resolved_topic,
                        "events_emitted": emitted,
                        "cycles": cycles,
                        "elapsed_sec": elapsed,
                        **snapshot_access.to_metadata(),
                    }
                    _write_replay_status(
                        status_client,
                        {
                            **base_status,
                            "status": "complete",
                            "events_emitted": emitted,
                            "cycles": cycles,
                            "finished_at": _now_iso(),
                            "data_ready": emitted > 0,
                        },
                    )
                    return result

                snapshot = _snapshot_from_row(row)
                if snapshot is None:
                    continue
                validate_market_snapshot(snapshot, raise_on_error=True)

                event = build_snapshot_event(
                    snapshot=snapshot,
                    source="snapshot_historical_replay",
                    metadata={
                        "replay": True,
                        "session_timezone": "IST",
                        "topic": resolved_topic,
                    },
                )
                publisher.publish(topic=resolved_topic, payload=event)
                emitted += 1
                replay_ts = _snapshot_timestamp(snapshot)
                trade_date = _snapshot_trade_date(snapshot) or start_date
                if emitted == 1 or emitted % 25 == 0:
                    _write_replay_status(
                        status_client,
                        {
                            **base_status,
                            "status": "running",
                            "events_emitted": emitted,
                            "cycles": cycles,
                            "current_replay_timestamp": replay_ts,
                            "current_trade_date": trade_date,
                            "virtual_time_current": replay_ts,
                            "data_ready": True,
                            "virtual_time_enabled": True,
                        },
                    )

                if out_path is not None:
                    with out_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

                if emitted % 500 == 0:
                    logger.info("historical replay emitted=%s topic=%s", emitted, resolved_topic)
                if sleep_sec > 0.0:
                    time.sleep(sleep_sec)

            if not loop:
                break

        elapsed = round(time.time() - started_at, 2)
        result = {
            "status": "complete",
            "topic": resolved_topic,
            "events_emitted": emitted,
            "cycles": cycles,
            "elapsed_sec": elapsed,
            **snapshot_access.to_metadata(),
        }
        _write_replay_status(
            status_client,
            {
                **base_status,
                "status": "complete",
                "events_emitted": emitted,
                "cycles": cycles,
                "finished_at": _now_iso(),
                "data_ready": emitted > 0,
            },
        )
        return result
    except Exception:
        _write_replay_status(
            status_client,
            {
                **base_status,
                "status": "failed",
                "events_emitted": emitted,
                "cycles": cycles,
                "finished_at": _now_iso(),
                "data_ready": emitted > 0,
            },
        )
        raise


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay historical snapshots to redis topic.")
    parser.add_argument("--base", default=str(DEFAULT_PARQUET_BASE), help=f"Parquet base path (default: {DEFAULT_PARQUET_BASE})")
    parser.add_argument("--topic", default=None, help="Destination topic (default: market:snapshot:v1:historical)")
    parser.add_argument("--start-date", default=None, help="Replay start date YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="Replay end date YYYY-MM-DD")
    parser.add_argument("--speed", type=float, default=0.0, help="Replay speed multiplier (0 = max speed, 1 = real time)")
    parser.add_argument("--loop", action="store_true", help="Loop date range continuously")
    parser.add_argument("--emit-jsonl", default=None, help="Optional event copy path (.jsonl)")
    parser.add_argument("--max-events", type=int, default=0, help="Stop after N emitted events (0 = no limit)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        result = replay_snapshots(
            parquet_base=args.base,
            topic=args.topic,
            start_date=args.start_date,
            end_date=args.end_date,
            speed=float(args.speed),
            loop=bool(args.loop),
            emit_jsonl=args.emit_jsonl,
            max_events=int(args.max_events),
        )
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))
    print(json.dumps(result, indent=2, default=str))
    return 0 if str(result.get("status")) in {"complete", "no_snapshots"} else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    raise SystemExit(run_cli())
