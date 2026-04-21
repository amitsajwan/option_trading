"""Replay persisted snapshot envelopes from Mongo to a Redis topic."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from pymongo import ASCENDING, MongoClient

from contracts_app import historical_snapshot_topic, parse_snapshot_event, snapshot_topic
from snapshot_app.redis_publisher import RedisEventPublisher

try:
    from snapshot_app.core.live_velocity_state import LiveVelocityAccumulator
except Exception:  # pragma: no cover - defensive, velocity module is optional
    LiveVelocityAccumulator = None  # type: ignore[assignment,misc]

try:
    from snapshot_app.historical.snapshot_access import DEFAULT_HISTORICAL_PARQUET_BASE as _DEFAULT_PARQUET_ROOT
except Exception:  # pragma: no cover
    _DEFAULT_PARQUET_ROOT = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MODE_CURRENT = "current"
_MODE_BASE_ONLY = "base_only"
_MODE_NO_IV_FILTER = "no_iv_filter"
_MODE_BASE_NO_IV_FILTER = "base_no_iv_filter"
_MODE_ML_SCORE_ALL = "ml_score_all"
_MODE_CHOICES = (
    _MODE_CURRENT,
    _MODE_BASE_ONLY,
    _MODE_NO_IV_FILTER,
    _MODE_BASE_NO_IV_FILTER,
    _MODE_ML_SCORE_ALL,
)


def _normalize_mode(mode: str) -> str:
    text = str(mode or _MODE_CURRENT).strip().lower()
    if text not in _MODE_CHOICES:
        raise ValueError(f"invalid mode '{mode}' (expected one of: {', '.join(_MODE_CHOICES)})")
    return text


def _mode_overrides(mode: str) -> dict[str, Any]:
    normalized = _normalize_mode(mode)
    if normalized == _MODE_CURRENT:
        return {}
    if normalized == _MODE_BASE_ONLY:
        # Force deterministic/base entry policy for this run (disables ML wrapper).
        return {"policy_config": {}}
    if normalized == _MODE_NO_IV_FILTER:
        # Remove IV_FILTER from entry path while keeping all other defaults.
        return {
            "router_config": {
                "enabled_entry_strategies": [
                    "ORB",
                    "EMA_CROSSOVER",
                    "OI_BUILDUP",
                    "PREV_DAY_LEVEL",
                    "VWAP_RECLAIM",
                    "HIGH_VOL_ORB",
                ]
            }
        }
    if normalized == _MODE_BASE_NO_IV_FILTER:
        return {
            "policy_config": {},
            "router_config": {
                "enabled_entry_strategies": [
                    "ORB",
                    "EMA_CROSSOVER",
                    "OI_BUILDUP",
                    "PREV_DAY_LEVEL",
                    "VWAP_RECLAIM",
                    "HIGH_VOL_ORB",
                ]
            },
        }
    if normalized == _MODE_ML_SCORE_ALL:
        return {"ml_score_all_snapshots": True}
    return {}


def _parse_matrix_modes(raw: Optional[str]) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return list(_MODE_CHOICES)
    items = [part.strip().lower() for part in text.split(",") if part.strip()]
    if not items:
        return list(_MODE_CHOICES)
    normalized = [_normalize_mode(item) for item in items]
    # Preserve order while removing duplicates.
    seen: set[str] = set()
    output: list[str] = []
    for item in normalized:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def _validate_date(value: str) -> str:
    text = str(value or "").strip()
    if not _DATE_RE.match(text):
        raise ValueError(f"invalid date '{value}' (expected YYYY-MM-DD)")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except Exception as exc:
        raise ValueError(f"invalid date '{value}' (expected YYYY-MM-DD)") from exc
    if parsed.strftime("%Y-%m-%d") != text:
        raise ValueError(f"invalid date '{value}' (expected YYYY-MM-DD)")
    return text


def _mongo_client(*, mongo_uri: Optional[str], mongo_host: str, mongo_port: int) -> MongoClient:
    uri = str(mongo_uri or "").strip()
    if uri:
        return MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000)
    return MongoClient(
        host=str(mongo_host or "localhost"),
        port=int(mongo_port),
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
    )


def _summary_counts(
    *,
    db: Any,
    run_id: str,
    vote_coll_name: str,
    signal_coll_name: str,
    position_coll_name: str,
    timeout_sec: int,
) -> dict[str, Any]:
    timeout = max(0, int(timeout_sec))
    started = time.time()
    elapsed = 0.0

    vote_coll = db[vote_coll_name]
    signal_coll = db[signal_coll_name]
    position_coll = db[position_coll_name]

    last = {"votes": 0, "signals": 0, "positions": 0}
    while True:
        counts = {
            "votes": int(vote_coll.count_documents({"run_id": run_id})),
            "signals": int(signal_coll.count_documents({"run_id": run_id})),
            "positions": int(position_coll.count_documents({"run_id": run_id})),
        }
        if counts == last and elapsed >= 0.5:
            last = counts
            break
        last = counts
        elapsed = max(0.0, time.time() - started)
        if elapsed >= timeout:
            break
        time.sleep(0.25)

    return {
        "status": "ok",
        "run_id": run_id,
        "wait_sec": round(max(0.0, time.time() - started), 3),
        "collections": {
            "strategy_votes_historical": {"name": vote_coll_name, "count": int(last["votes"])},
            "trade_signals_historical": {"name": signal_coll_name, "count": int(last["signals"])},
            "strategy_positions_historical": {"name": position_coll_name, "count": int(last["positions"])},
        },
    }


def replay_from_mongo(
    *,
    trade_date: str,
    topic: Optional[str] = None,
    run_id: Optional[str] = None,
    speed: float = 0.0,
    max_events: int = 0,
    dry_run: bool = False,
    emit_jsonl: Optional[str] = None,
    mongo_uri: Optional[str] = None,
    mongo_host: str = "localhost",
    mongo_port: int = 27017,
    mongo_db: str = "trading_ai",
    mongo_coll: str = "phase1_market_snapshots",
    summary: bool = True,
    summary_timeout_sec: int = 5,
    allow_live_topic: bool = False,
    mode: str = _MODE_CURRENT,
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """Replay one trade date from Mongo snapshot collection."""
    validated_date = _validate_date(trade_date)
    normalized_mode = _normalize_mode(mode)
    resolved_topic = str(topic or historical_snapshot_topic()).strip() or historical_snapshot_topic()
    resolved_run_id = str(run_id or f"mongo-replay-{validated_date}-{uuid.uuid4().hex[:8]}").strip()
    if not resolved_run_id:
        resolved_run_id = f"mongo-replay-{validated_date}-{uuid.uuid4().hex[:8]}"

    live_topic = snapshot_topic()
    if resolved_topic == live_topic and not bool(allow_live_topic):
        raise RuntimeError(
            f"refusing live topic '{live_topic}'. Use --allow-live-topic to override explicitly."
        )

    out_path = Path(emit_jsonl).resolve() if emit_jsonl else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    client = _mongo_client(mongo_uri=mongo_uri, mongo_host=mongo_host, mongo_port=int(mongo_port))
    try:
        client.admin.command("ping")
        db = client[str(mongo_db or "trading_ai").strip() or "trading_ai"]
        coll = db[str(mongo_coll or "phase1_market_snapshots").strip() or "phase1_market_snapshots"]
        query = {"trade_date_ist": validated_date}
        source_count = int(coll.count_documents(query))
        if source_count <= 0:
            replay_result = {
                "status": "no_data_for_date",
                "date": validated_date,
                "mode": normalized_mode,
                "topic": resolved_topic,
                "run_id": resolved_run_id,
                "source_count": 0,
                "emitted_count": 0,
                "skipped_count": 0,
                "first_snapshot_id": None,
                "last_snapshot_id": None,
            }
            return replay_result, None

        cursor = coll.find(query, {"_id": 0, "snapshot_id": 1, "timestamp": 1, "payload": 1}).sort(
            [("timestamp", ASCENDING), ("snapshot_id", ASCENDING)]
        )

        events_limit = max(0, int(max_events))
        sleep_sec = 0.0 if float(speed) <= 0 else (60.0 / float(speed))
        publisher = RedisEventPublisher() if not bool(dry_run) else None

        # Velocity enrichment: replayed envelopes captured before the live
        # builder injected `velocity_enrichment` will be missing the 30 V2
        # features. Re-inject here to preserve training/inference parity for
        # V2 staged bundles. Mongo replay runs one date at a time in timestamp
        # order, so a fresh accumulator per invocation is correct.
        velocity_parquet_root_raw = str(os.getenv("SNAPSHOT_PARQUET_ROOT") or "").strip()
        velocity_parquet_root: Optional[Path] = None
        if velocity_parquet_root_raw and Path(velocity_parquet_root_raw).exists():
            velocity_parquet_root = Path(velocity_parquet_root_raw)
        elif _DEFAULT_PARQUET_ROOT is not None and Path(_DEFAULT_PARQUET_ROOT).exists():
            velocity_parquet_root = Path(_DEFAULT_PARQUET_ROOT)
        velocity_acc = (
            LiveVelocityAccumulator(parquet_root=velocity_parquet_root)
            if LiveVelocityAccumulator is not None
            else None
        )
        if velocity_acc is None:
            logger.warning(
                "mongo replay: LiveVelocityAccumulator unavailable; "
                "velocity_enrichment will NOT be injected into replayed snapshots",
            )

        emitted = 0
        skipped = 0
        seen_snapshot_ids: set[str] = set()
        first_snapshot_id: Optional[str] = None
        last_snapshot_id: Optional[str] = None
        started_at = time.time()

        for row in cursor:
            if events_limit > 0 and emitted >= events_limit:
                break

            payload = row.get("payload")
            if not isinstance(payload, dict):
                skipped += 1
                continue
            valid_event = parse_snapshot_event(payload)
            if valid_event is None:
                skipped += 1
                continue

            snapshot_id = str(valid_event.get("snapshot_id") or "").strip()
            if not snapshot_id:
                skipped += 1
                continue
            if snapshot_id in seen_snapshot_ids:
                skipped += 1
                continue
            seen_snapshot_ids.add(snapshot_id)

            event = copy.deepcopy(valid_event)

            # Inject velocity_enrichment before publishing so downstream V2
            # staged-runtime consumers see the same snapshot shape as live
            # inference. If the envelope already carries velocity_enrichment
            # (captured by a live builder with the accumulator wired in), the
            # accumulator's `process()` is idempotent and will recompute /
            # overwrite with the identical values once 11:30 is reached.
            if velocity_acc is not None:
                inner = event.get("snapshot")
                if isinstance(inner, dict):
                    try:
                        event["snapshot"] = velocity_acc.process(inner)
                    except Exception:
                        logger.exception(
                            "mongo replay: velocity accumulator failed for snapshot_id=%s; "
                            "publishing without velocity_enrichment",
                            snapshot_id,
                        )

            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            metadata = dict(metadata)
            metadata["run_id"] = resolved_run_id
            metadata["replay"] = True
            metadata["replay_source"] = "mongo_phase1_market_snapshots"
            metadata["replay_date"] = validated_date
            metadata["mode"] = normalized_mode
            metadata["topic"] = resolved_topic
            metadata.update(_mode_overrides(normalized_mode))
            event["metadata"] = metadata

            if first_snapshot_id is None:
                first_snapshot_id = snapshot_id
            last_snapshot_id = snapshot_id

            if out_path is not None:
                with out_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

            if not bool(dry_run) and publisher is not None:
                publisher.publish(topic=resolved_topic, payload=event)
            emitted += 1

            if emitted % 500 == 0:
                logger.info("mongo replay emitted=%s topic=%s run_id=%s", emitted, resolved_topic, resolved_run_id)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        replay_result = {
            "status": "dry_run" if bool(dry_run) else "complete",
            "date": validated_date,
            "mode": normalized_mode,
            "topic": resolved_topic,
            "run_id": resolved_run_id,
            "source_count": source_count,
            "emitted_count": emitted,
            "skipped_count": skipped,
            "first_snapshot_id": first_snapshot_id,
            "last_snapshot_id": last_snapshot_id,
            "elapsed_sec": round(max(0.0, time.time() - started_at), 3),
        }

        summary_result: Optional[dict[str, Any]] = None
        if bool(summary):
            vote_coll_name = (
                str(os.getenv("MONGO_COLL_STRATEGY_VOTES_HISTORICAL") or "strategy_votes_historical").strip()
                or "strategy_votes_historical"
            )
            signal_coll_name = (
                str(os.getenv("MONGO_COLL_TRADE_SIGNALS_HISTORICAL") or "trade_signals_historical").strip()
                or "trade_signals_historical"
            )
            position_coll_name = (
                str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL") or "strategy_positions_historical").strip()
                or "strategy_positions_historical"
            )
            summary_result = _summary_counts(
                db=db,
                run_id=resolved_run_id,
                vote_coll_name=vote_coll_name,
                signal_coll_name=signal_coll_name,
                position_coll_name=position_coll_name,
                timeout_sec=int(summary_timeout_sec),
            )

        return replay_result, summary_result
    finally:
        client.close()


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay persisted Mongo snapshot envelopes to Redis topic.")
    parser.add_argument("--date", required=True, help="Trade date YYYY-MM-DD")
    parser.add_argument("--topic", default=None, help="Destination topic (default: market:snapshot:v1:historical)")
    parser.add_argument("--run-id", default=None, help="Optional run_id (default: generated)")
    parser.add_argument("--speed", type=float, default=0.0, help="Replay speed multiplier (0 = max speed, 1 = real time)")
    parser.add_argument("--max-events", type=int, default=0, help="Stop after N emitted events (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true", help="Validate and scan only; do not publish")
    parser.add_argument("--emit-jsonl", default=None, help="Optional event copy path (.jsonl)")
    parser.add_argument("--mongo-uri", default=None, help="Mongo URI override")
    parser.add_argument("--mongo-host", default=str(os.getenv("MONGO_HOST") or "localhost"))
    parser.add_argument("--mongo-port", type=int, default=int(os.getenv("MONGO_PORT") or "27017"))
    parser.add_argument("--mongo-db", default=str(os.getenv("MONGO_DB") or "trading_ai"))
    parser.add_argument(
        "--mongo-coll",
        default=str(os.getenv("MONGO_COLL_SNAPSHOTS") or "phase1_market_snapshots"),
        help="Source collection containing snapshot envelopes",
    )
    parser.add_argument("--summary", action="store_true", default=True, help="Print run-scoped historical summary")
    parser.add_argument("--no-summary", action="store_false", dest="summary", help="Disable post-replay summary")
    parser.add_argument("--summary-timeout-sec", type=int, default=5, help="Summary polling timeout in seconds")
    parser.add_argument(
        "--mode",
        default=_MODE_CURRENT,
        choices=list(_MODE_CHOICES),
        help=(
            "Replay mode override: "
            "current (default), base_only (disable ML wrapper), "
            "no_iv_filter (remove IV_FILTER from entry path), "
            "base_no_iv_filter (base_only + no_iv_filter), "
            "ml_score_all (shadow-score every snapshot with ML)"
        ),
    )
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="Run multiple modes in sequence and print per-mode results",
    )
    parser.add_argument(
        "--matrix-modes",
        default=None,
        help="Comma-separated mode list for --matrix (default: all modes)",
    )
    parser.add_argument(
        "--allow-live-topic",
        action="store_true",
        help="Allow publishing to live snapshot topic explicitly (blocked by default)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if bool(args.matrix):
        try:
            matrix_modes = _parse_matrix_modes(args.matrix_modes)
            base_run = str(args.run_id or f"mongo-replay-{args.date}-{uuid.uuid4().hex[:8]}").strip()
            matrix_output: list[dict[str, Any]] = []
            overall_status = 0
            for mode_name in matrix_modes:
                run_id = f"{base_run}-{mode_name}"
                replay_result, summary_result = replay_from_mongo(
                    trade_date=str(args.date),
                    topic=args.topic,
                    run_id=run_id,
                    speed=float(args.speed),
                    max_events=int(args.max_events),
                    dry_run=bool(args.dry_run),
                    emit_jsonl=args.emit_jsonl,
                    mongo_uri=args.mongo_uri,
                    mongo_host=str(args.mongo_host),
                    mongo_port=int(args.mongo_port),
                    mongo_db=str(args.mongo_db),
                    mongo_coll=str(args.mongo_coll),
                    summary=bool(args.summary),
                    summary_timeout_sec=int(args.summary_timeout_sec),
                    allow_live_topic=bool(args.allow_live_topic),
                    mode=mode_name,
                )
                matrix_output.append(
                    {
                        "mode": mode_name,
                        "replay": replay_result,
                        "summary": summary_result,
                    }
                )
                status = str(replay_result.get("status") or "")
                if status == "no_data_for_date":
                    overall_status = max(overall_status, 2)
                elif status not in {"complete", "dry_run"}:
                    overall_status = max(overall_status, 1)
            print(json.dumps({"status": "matrix_complete", "results": matrix_output}, indent=2, ensure_ascii=False, default=str))
            return int(overall_status)
        except Exception as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, indent=2, ensure_ascii=False, default=str))
            return 1

    try:
        replay_result, summary_result = replay_from_mongo(
            trade_date=str(args.date),
            topic=args.topic,
            run_id=args.run_id,
            speed=float(args.speed),
            max_events=int(args.max_events),
            dry_run=bool(args.dry_run),
            emit_jsonl=args.emit_jsonl,
            mongo_uri=args.mongo_uri,
            mongo_host=str(args.mongo_host),
            mongo_port=int(args.mongo_port),
            mongo_db=str(args.mongo_db),
            mongo_coll=str(args.mongo_coll),
            summary=bool(args.summary),
            summary_timeout_sec=int(args.summary_timeout_sec),
            allow_live_topic=bool(args.allow_live_topic),
            mode=str(args.mode),
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2, ensure_ascii=False, default=str))
        return 1

    print(json.dumps(replay_result, indent=2, ensure_ascii=False, default=str))
    if summary_result is not None:
        print(json.dumps(summary_result, indent=2, ensure_ascii=False, default=str))

    status = str(replay_result.get("status") or "")
    if status == "no_data_for_date":
        return 2
    return 0 if status in {"complete", "dry_run"} else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    raise SystemExit(run_cli())
