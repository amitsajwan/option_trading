from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import redis
from pymongo import MongoClient

from contracts_app import build_snapshot_event, historical_snapshot_topic
from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.snapshot_access import (
    DEFAULT_HISTORICAL_PARQUET_BASE,
    SNAPSHOT_DATASET_LEGACY_RAW,
    SNAPSHOT_INPUT_MODE_LEGACY_RAW,
    require_snapshot_access,
)

logger = logging.getLogger(__name__)
ROLLOUT_STAGES = {"paper", "shadow", "capped_live"}
MIN_PAPER_DAYS = 10
MIN_SHADOW_DAYS = 10
DEFAULT_CAPPED_LIVE_SIZE_MULTIPLIER = 0.25
DEFAULT_HALT_CONSECUTIVE_LOSSES = 3
DEFAULT_HALT_DAILY_DD_PCT = -0.75


def validate_rollout_command(
    *,
    rollout_stage: str,
    paper_days_observed: int,
    shadow_days_observed: int,
    position_size_multiplier: float,
    ml_runtime_enabled: bool = False,
    offline_strict_positive_passed: bool = False,
    approved_for_runtime: bool = False,
) -> Optional[str]:
    stage = str(rollout_stage or "").strip().lower()
    if stage not in ROLLOUT_STAGES:
        return f"invalid rollout_stage '{stage}'"
    if stage in {"shadow", "capped_live"} and int(paper_days_observed) < MIN_PAPER_DAYS:
        return f"rollout requires >= {MIN_PAPER_DAYS} paper days before {stage}"
    if stage == "capped_live" and int(shadow_days_observed) < MIN_SHADOW_DAYS:
        return f"capped_live requires >= {MIN_SHADOW_DAYS} shadow days"
    if stage == "capped_live" and float(position_size_multiplier) > DEFAULT_CAPPED_LIVE_SIZE_MULTIPLIER:
        return f"capped_live position_size_multiplier must be <= {DEFAULT_CAPPED_LIVE_SIZE_MULTIPLIER}"
    if bool(ml_runtime_enabled):
        if stage != "capped_live":
            return "ml runtime is allowed only in capped_live stage"
        if not bool(offline_strict_positive_passed):
            return "ml runtime requires offline_strict_positive_passed=true"
        if not bool(approved_for_runtime):
            return "ml runtime requires approved_for_runtime=true"
    return None


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _redis_client() -> redis.Redis:
    return redis.Redis(
        host=str(os.getenv("REDIS_HOST") or "localhost"),
        port=int(os.getenv("REDIS_PORT") or "6379"),
        db=int(os.getenv("REDIS_DB") or "0"),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _mongo_collection():
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        client = MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000)
    else:
        client = MongoClient(
            host=str(os.getenv("MONGO_HOST") or "localhost"),
            port=int(os.getenv("MONGO_PORT") or "27017"),
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=5000,
        )
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    coll_name = str(os.getenv("MONGO_COLL_STRATEGY_EVAL_RUNS") or "strategy_eval_runs")
    return client, client[db_name][coll_name]


def _run_channel(run_id: str) -> str:
    prefix = str(os.getenv("STRATEGY_EVAL_RUN_CHANNEL_PREFIX") or "strategy:eval:run:")
    return f"{prefix}{run_id}"


def _global_channel() -> str:
    return str(os.getenv("STRATEGY_EVAL_GLOBAL_CHANNEL") or "strategy:eval:global")


def _command_channel() -> str:
    return str(os.getenv("STRATEGY_EVAL_COMMAND_TOPIC") or "strategy:eval:command")


def _historical_topic() -> str:
    return str(os.getenv("HISTORICAL_TOPIC") or historical_snapshot_topic()).strip() or historical_snapshot_topic()


def _default_snapshot_parquet_base() -> Path:
    return Path(os.getenv("SNAPSHOT_PARQUET_BASE") or DEFAULT_HISTORICAL_PARQUET_BASE)


def _update_run(coll: Any, run_id: str, **fields: Any) -> None:
    fields["updated_at"] = _utc_now()
    coll.update_one({"run_id": str(run_id)}, {"$set": fields}, upsert=False)


def _publish_run_event(redis_client: redis.Redis, run_id: str, payload: dict[str, Any]) -> None:
    body = dict(payload or {})
    body["run_id"] = str(run_id)
    body["timestamp"] = _utc_now()
    rendered = json.dumps(body, ensure_ascii=False, default=str)
    redis_client.publish(_run_channel(run_id), rendered)
    redis_client.publish(_global_channel(), rendered)


def _parse_snapshot_raw(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _replay_and_publish(
    *,
    redis_client: redis.Redis,
    run_id: str,
    date_from: str,
    date_to: str,
    base_path: str,
    speed: float,
    risk_config: Optional[dict[str, Any]] = None,
    rollout_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    topic = _historical_topic()
    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_LEGACY_RAW,
        context="strategy_eval_orchestrator",
        parquet_base=Path(base_path),
        min_day=date_from,
        max_day=date_to,
    )
    store = ParquetStore(base_path, snapshots_dataset=SNAPSHOT_DATASET_LEGACY_RAW)
    frame = store.snapshots_for_date_range(date_from, date_to)
    if len(frame) == 0:
        return {"status": "no_snapshots", "events_emitted": 0, **snapshot_access.to_metadata()}
    frame["timestamp"] = frame.get("timestamp")
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    total = int(len(frame))
    sleep_sec = 0.0 if float(speed) <= 0 else (60.0 / float(speed))
    emitted = 0
    last_pct = -1

    _publish_run_event(
        redis_client,
        run_id,
        {
            "event_type": "run_started",
            "progress_pct": 0.0,
            "current_day": date_from,
            "total_days": None,
            "message": f"Replay started for {date_from} to {date_to}",
        },
    )

    for idx, row in frame.iterrows():
        snapshot = _parse_snapshot_raw(row.get("snapshot_raw_json"))
        if snapshot is None:
            continue
        event = build_snapshot_event(
            snapshot=snapshot,
            source="strategy_eval_orchestrator",
            metadata={
                "replay": True,
                "run_id": run_id,
                "topic": topic,
                "risk_config": dict(risk_config or {}),
                "rollout_context": dict(rollout_context or {}),
            },
        )
        redis_client.publish(topic, json.dumps(event, ensure_ascii=False, default=str))
        emitted += 1
        pct = int(((idx + 1) / max(1, total)) * 100)
        if pct != last_pct and (pct % 5 == 0 or pct == 100):
            last_pct = pct
            current_day = str(row.get("trade_date") or row.get("date") or "")[:10] or None
            _publish_run_event(
                redis_client,
                run_id,
                {
                    "event_type": "run_progress",
                    "progress_pct": float(pct),
                    "current_day": current_day,
                    "total_days": None,
                    "message": f"Replay progress {pct}%",
                },
            )
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    return {"status": "complete", "events_emitted": emitted, **snapshot_access.to_metadata()}


def _process_command(redis_client: redis.Redis, coll: Any, command: dict[str, Any]) -> None:
    run_id = str(command.get("run_id") or "").strip()
    if not run_id:
        return
    date_from = str(command.get("date_from") or "").strip()
    date_to = str(command.get("date_to") or "").strip()
    dataset = str(command.get("dataset") or "historical").strip().lower()
    speed = float(command.get("speed") or 0.0)
    base_path = str(command.get("base_path") or "").strip() or str(_default_snapshot_parquet_base())
    risk_config = dict(command.get("risk_config") or {}) if isinstance(command.get("risk_config"), dict) else {}
    rollout_stage = str(command.get("rollout_stage") or "paper").strip().lower()
    paper_days_observed = int(command.get("paper_days_observed") or 0)
    shadow_days_observed = int(command.get("shadow_days_observed") or 0)
    position_size_multiplier = float(command.get("position_size_multiplier") or DEFAULT_CAPPED_LIVE_SIZE_MULTIPLIER)
    halt_consecutive_losses = int(command.get("halt_consecutive_losses") or DEFAULT_HALT_CONSECUTIVE_LOSSES)
    halt_daily_dd_pct = float(command.get("halt_daily_dd_pct") or DEFAULT_HALT_DAILY_DD_PCT)
    ml_runtime_enabled = bool(command.get("ml_runtime_enabled"))
    offline_strict_positive_passed = bool(command.get("offline_strict_positive_passed"))
    approved_for_runtime = bool(command.get("approved_for_runtime"))

    if dataset != "historical":
        _update_run(coll, run_id, status="failed", ended_at=_utc_now(), error=f"unsupported dataset '{dataset}'")
        _publish_run_event(
            redis_client,
            run_id,
            {"event_type": "run_failed", "message": "Unsupported dataset", "error": f"dataset={dataset}"},
        )
        return
    if not date_from or not date_to:
        _update_run(coll, run_id, status="failed", ended_at=_utc_now(), error="date_from/date_to missing")
        _publish_run_event(
            redis_client,
            run_id,
            {"event_type": "run_failed", "message": "Missing date range", "error": "date_from/date_to required"},
        )
        return
    rollout_error = validate_rollout_command(
        rollout_stage=rollout_stage,
        paper_days_observed=paper_days_observed,
        shadow_days_observed=shadow_days_observed,
        position_size_multiplier=position_size_multiplier,
        ml_runtime_enabled=ml_runtime_enabled,
        offline_strict_positive_passed=offline_strict_positive_passed,
        approved_for_runtime=approved_for_runtime,
    )
    if rollout_error:
        _update_run(
            coll,
            run_id,
            status="failed",
            ended_at=_utc_now(),
            error=rollout_error,
        )
        _publish_run_event(
            redis_client,
            run_id,
            {
                "event_type": "run_failed",
                "message": "Rollout validation failed",
                "error": rollout_error,
            },
        )
        return

    rollout_context = {
        "rollout_stage": rollout_stage,
        "paper_days_observed": paper_days_observed,
        "shadow_days_observed": shadow_days_observed,
        "position_size_multiplier": position_size_multiplier,
        "halt_consecutive_losses": halt_consecutive_losses,
        "halt_daily_dd_pct": halt_daily_dd_pct,
        "ml_runtime_enabled": ml_runtime_enabled,
        "offline_strict_positive_passed": offline_strict_positive_passed,
        "approved_for_runtime": approved_for_runtime,
    }

    _update_run(
        coll,
        run_id,
        status="running",
        started_at=_utc_now(),
        error=None,
        message=f"Running replay for {date_from} to {date_to}",
        progress_pct=0.0,
    )
    try:
        result = _replay_and_publish(
            redis_client=redis_client,
            run_id=run_id,
            date_from=date_from,
            date_to=date_to,
            base_path=base_path,
            speed=float(speed),
            risk_config=risk_config,
            rollout_context=rollout_context,
        )
    except Exception as exc:
        _update_run(coll, run_id, status="failed", ended_at=_utc_now(), error=str(exc), message="Replay failed")
        _publish_run_event(
            redis_client,
            run_id,
            {"event_type": "run_failed", "message": "Replay failed", "error": str(exc)},
        )
        return

    status = str(result.get("status") or "complete")
    if status in {"complete", "no_snapshots"}:
        _update_run(
            coll,
            run_id,
            status="completed",
            ended_at=_utc_now(),
            progress_pct=100.0,
            message=f"Replay finished: emitted={int(result.get('events_emitted') or 0)}",
            error=None,
        )
        _publish_run_event(
            redis_client,
            run_id,
            {
                "event_type": "run_completed",
                "progress_pct": 100.0,
                "message": f"Replay completed ({int(result.get('events_emitted') or 0)} events)",
            },
        )
        _publish_run_event(
            redis_client,
            run_id,
            {
                "event_type": "evaluation_ready",
                "progress_pct": 100.0,
                "message": "Evaluation data refreshed",
            },
        )
        return

    _update_run(coll, run_id, status="failed", ended_at=_utc_now(), error="Unknown replay status")
    _publish_run_event(redis_client, run_id, {"event_type": "run_failed", "message": "Unknown replay status"})


def run_loop() -> int:
    redis_client = _redis_client()
    mongo_client, runs_coll = _mongo_collection()
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(_command_channel())
    logger.info("strategy_eval_orchestrator subscribed topic=%s", _command_channel())
    try:
        while True:
            msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not msg:
                time.sleep(0.01)
                continue
            data = msg.get("data")
            if not isinstance(data, str):
                continue
            try:
                payload = json.loads(data)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            _process_command(redis_client, runs_coll, payload)
    except KeyboardInterrupt:
        logger.info("strategy_eval_orchestrator interrupted")
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
        try:
            mongo_client.close()
        except Exception:
            pass
    return 0


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Strategy evaluation replay orchestrator")
    parser.parse_args(list(argv) if argv is not None else None)
    return run_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    raise SystemExit(run_cli())
