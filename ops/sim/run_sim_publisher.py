"""Sim publisher CLI — XADDs recorded snapshots onto a per-run Redis Stream.

Reads a day's snapshots from a source Mongo collection, wraps each with the
sim discriminator metadata (`source_mode`, `run_id`, `sim_label`), and XADDs
them to ``stream:snapshots:sim:<run_id>`` at a controlled rate. Emits a
sentinel event at end-of-corpus so the consumer can exit cleanly.

Designed for orchestrator-spawned use, but also runnable directly as a CLI
for smoke testing.

Manifest behaviour:
    - Writes an immutable ``manifest.json`` into the run dir at startup.
    - On SIGINT/SIGTERM, drops a ``cancellation.json`` alongside (never
      overwrites the manifest — manifests are write-once). Sentinel is
      published with ``aborted=1``.
    - On clean end-of-corpus, drops a ``result.json`` with the final stats.
    - The orchestrator (SIM-6) reads ``result.json`` / ``cancellation.json``
      and reflects status into the ``strategy_eval_runs`` registry.

See also:
    docs/SCRUM_BOARD_SIM_REPLAY.md  (SIM-3)
    memory/project_sim_replay_design_2026-05-27  (design doc)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Protocol

from contracts_app import (
    SimManifest,
    compute_config_hash,
    isoformat_ist,
    now_ist,
    resolve_git_commit,
    resolve_namespace,
)

logger = logging.getLogger("ops.sim.run_sim_publisher")

DEFAULT_SOURCE_COLL = "phase1_market_snapshots"
DEFAULT_SPEED = 30.0
DEFAULT_MAX_LEN = 10_000
DEFAULT_EVENT_KIND = "snapshots"
SENTINEL_TYPE = "sentinel"


# ── Client protocols (dependency-injected so tests can mock cheaply) ──────


class _RedisLike(Protocol):
    def xadd(
        self,
        name: str,
        fields: Mapping[str, Any],
        *,
        maxlen: Optional[int] = ...,
        approximate: bool = ...,
    ) -> str:
        ...


class _CollectionLike(Protocol):
    def find(
        self,
        filter: Mapping[str, Any],
        projection: Optional[Mapping[str, Any]] = ...,
    ) -> Iterable[Mapping[str, Any]]:
        ...

    def count_documents(self, filter: Mapping[str, Any]) -> int:  # noqa: A002
        ...


# ── Manifest helpers ──────────────────────────────────────────────────────


def _write_cancellation(run_dir: Path, reason: str, sentinel_id: Optional[str]) -> Path:
    """Atomic write-once cancellation marker; never touches manifest.json."""
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "cancellation.json"
    if target.exists():
        return target  # idempotent; first cancellation wins
    payload = {
        "cancelled_at": isoformat_ist(now_ist()),
        "reason": reason,
        "sentinel_id": sentinel_id,
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def _write_result(
    run_dir: Path,
    *,
    total_published: int,
    sentinel_id: str,
    terminal_status: str,
) -> Path:
    """Atomic write-once result marker for a successfully drained sim."""
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "result.json"
    if target.exists():
        return target
    payload = {
        "completed_at": isoformat_ist(now_ist()),
        "total_published": int(total_published),
        "sentinel_id": sentinel_id,
        "terminal_status": terminal_status,
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


# ── Publisher ─────────────────────────────────────────────────────────────


@dataclass
class _State:
    aborted: bool = False
    last_sentinel_id: Optional[str] = None


class SimPublisher:
    """XADDs snapshots from a Mongo source coll to a per-run Redis Stream.

    Designed for one publisher per run_id. Reusing across runs is not
    supported by design (single ``run_id`` field on the instance).
    """

    def __init__(
        self,
        *,
        run_id: str,
        source_date: str,
        source_coll: str,
        label: str,
        speed: float,
        max_len: int,
        redis_client: _RedisLike,
        mongo_collection: _CollectionLike,
        image_digest: str = "unknown",
        env_overrides: Optional[Mapping[str, str]] = None,
        event_kind: str = DEFAULT_EVENT_KIND,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
    ) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        if not source_date:
            raise ValueError("source_date is required")
        if not source_coll:
            raise ValueError("source_coll is required")
        if float(speed) <= 0:
            raise ValueError("speed must be > 0")
        if int(max_len) < 1:
            raise ValueError("max_len must be >= 1")

        self.run_id = run_id
        self.source_date = source_date
        self.source_coll_name = source_coll
        self.label = label
        self.speed = float(speed)
        self.max_len = int(max_len)
        self._redis = redis_client
        self._coll = mongo_collection
        self._image_digest = image_digest
        self._env_overrides = dict(env_overrides or {})
        self._event_kind = event_kind
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn

        self._namespace = resolve_namespace("sim", run_id=run_id)
        self._stream_name = self._namespace.stream_for(event_kind)
        self._run_dir = self._namespace.run_dir_for()
        self._state = _State()

    # ── manifest ─────────────────────────────────────────────────────────
    def write_initial_manifest(self) -> Path:
        """Write the run's manifest.json. Idempotent only if identical;
        raises FileExistsError otherwise (manifests are immutable)."""
        config_hash = compute_config_hash(
            env_overrides=self._env_overrides,
            image_digest=self._image_digest,
            speed=self.speed,
        )
        manifest = SimManifest(
            run_id=self.run_id,
            kind="sim",
            source_date=self.source_date,
            source_coll=self.source_coll_name,
            label=self.label,
            git_commit=resolve_git_commit(),
            config_hash=config_hash,
            env_overrides=self._env_overrides,
            image_digest=self._image_digest,
            speed=self.speed,
            created_at=isoformat_ist(now_ist()),
        )
        target = self._run_dir / "manifest.json"
        if target.exists():
            try:
                existing = SimManifest.from_json(target.read_text(encoding="utf-8"))
                if (
                    existing.run_id == manifest.run_id
                    and existing.source_date == manifest.source_date
                    and existing.source_coll == manifest.source_coll
                    and existing.config_hash == manifest.config_hash
                ):
                    return target
            except Exception:
                pass
            raise FileExistsError(
                f"manifest already exists at {target}; manifests are immutable"
            )
        return manifest.write_to(self._run_dir)

    # ── lifecycle ────────────────────────────────────────────────────────
    @property
    def stream_name(self) -> str:
        return self._stream_name

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def request_stop(self, reason: str = "signal") -> None:
        """Signal the publish loop to break and emit an aborted sentinel."""
        if not self._state.aborted:
            self._state.aborted = True
            logger.warning(
                "sim publisher abort requested run_id=%s reason=%s",
                self.run_id,
                reason,
            )

    def count_source(self) -> int:
        return int(self._coll.count_documents({"trade_date_ist": self.source_date}))

    def _iter_source(self) -> Iterator[Mapping[str, Any]]:
        cursor = self._coll.find(
            {"trade_date_ist": self.source_date},
            None,  # no projection: pass-through full payload
        )
        for doc in cursor:
            yield doc

    def _wrap_event(self, doc: Mapping[str, Any]) -> dict[str, str]:
        """Build the XADD field dict.

        Redis Streams field values are bytes/str. We carry the full snapshot
        payload as a JSON-encoded string under ``payload`` plus the
        discriminator fields at the top level for cheap inspection without
        a JSON decode.
        """
        payload = dict(doc)
        meta = {
            "source_mode": "sim",
            "run_id": self.run_id,
            "sim_label": self.label,
        }
        # Inject meta both at the top level of payload and as separate
        # stream fields so consumers reading via XINFO / XRANGE can filter
        # without deserialising.
        if isinstance(payload.get("meta"), dict):
            payload["meta"] = {**payload["meta"], **meta}
        else:
            payload["meta"] = meta
        # _id is a BSON ObjectId — coerce to str for JSON safety
        if "_id" in payload:
            payload["_id"] = str(payload["_id"])
        return {
            "type": "snapshot",
            "run_id": self.run_id,
            "sim_label": self.label,
            "source_mode": "sim",
            "snapshot_id": str(payload.get("snapshot_id") or ""),
            "payload": json.dumps(payload, default=str),
        }

    def _emit_sentinel(self, *, total_published: int) -> str:
        fields = {
            "type": SENTINEL_TYPE,
            "run_id": self.run_id,
            "aborted": "1" if self._state.aborted else "0",
            "total_published": str(int(total_published)),
            "emitted_at": isoformat_ist(now_ist()),
        }
        entry_id = self._redis.xadd(
            self._stream_name,
            fields,
            maxlen=self.max_len,
            approximate=True,
        )
        self._state.last_sentinel_id = entry_id
        logger.info(
            "sim publisher sentinel run_id=%s aborted=%s total=%d entry_id=%s",
            self.run_id,
            self._state.aborted,
            total_published,
            entry_id,
        )
        return entry_id

    def run(self) -> dict[str, Any]:
        """Drain source → stream. Returns a summary dict.

        On abort: sentinel emitted with ``aborted=1``, cancellation.json
        dropped, summary indicates cancellation. On clean completion:
        sentinel with ``aborted=0``, result.json dropped.
        """
        published = 0
        bar_interval_sec = 60.0 / self.speed
        next_emit_monotonic = self._monotonic()
        try:
            for doc in self._iter_source():
                if self._state.aborted:
                    break
                fields = self._wrap_event(doc)
                self._redis.xadd(
                    self._stream_name,
                    fields,
                    maxlen=self.max_len,
                    approximate=True,
                )
                published += 1

                # Pace based on monotonic clock so we don't drift on slow XADD.
                next_emit_monotonic += bar_interval_sec
                drift = next_emit_monotonic - self._monotonic()
                if drift > 0:
                    self._sleep(drift)
                else:
                    # We're already behind — don't sleep, reset baseline.
                    next_emit_monotonic = self._monotonic()
        finally:
            sentinel_id = self._emit_sentinel(total_published=published)
            terminal = "cancelled" if self._state.aborted else "completed"
            if self._state.aborted:
                _write_cancellation(self._run_dir, reason="aborted_during_publish", sentinel_id=sentinel_id)
            else:
                _write_result(
                    self._run_dir,
                    total_published=published,
                    sentinel_id=sentinel_id,
                    terminal_status=terminal,
                )

        return {
            "run_id": self.run_id,
            "stream": self._stream_name,
            "total_published": published,
            "terminal_status": terminal,
            "sentinel_id": self._state.last_sentinel_id,
        }


# ── CLI ───────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_sim_publisher",
        description="Publish recorded snapshots to a per-run Redis Stream for sim/replay.",
    )
    p.add_argument("--run-id", required=True, help="UUIDv7 (or any string) identifying the run")
    p.add_argument("--source-date", required=True, help="trade_date_ist YYYY-MM-DD")
    p.add_argument(
        "--source-coll",
        default=DEFAULT_SOURCE_COLL,
        help=f"Mongo collection to read from (default: {DEFAULT_SOURCE_COLL})",
    )
    p.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_SPEED,
        help=f"bars per minute equivalent; sleep = 60/speed (default: {DEFAULT_SPEED})",
    )
    p.add_argument("--label", default="", help="human-readable tag stored in the manifest")
    p.add_argument(
        "--max-len",
        type=int,
        default=DEFAULT_MAX_LEN,
        help=f"Redis XADD MAXLEN ~ approximation (default: {DEFAULT_MAX_LEN})",
    )
    p.add_argument(
        "--image-digest",
        default=os.getenv("STRATEGY_APP_IMAGE_DIGEST", "unknown"),
        help="strategy_app image digest for the manifest (orchestrator usually supplies this)",
    )
    p.add_argument(
        "--env-overrides-json",
        default="{}",
        help="JSON-encoded dict of strategy_app env overrides being tested",
    )
    p.add_argument("--log-level", default="INFO")
    return p


def _connect_redis() -> Any:
    import redis  # local import to keep module-load fast in unit tests

    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    db = int(os.getenv("REDIS_DB", "0"))
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def _connect_mongo_collection(coll_name: str) -> Any:
    from pymongo import MongoClient  # local import

    uri = os.getenv("MONGO_URI") or (
        f"mongodb://{os.getenv('MONGO_HOST', 'localhost')}:{os.getenv('MONGO_PORT', '27017')}"
    )
    db_name = os.getenv("MONGO_DB", "trading_ai")
    return MongoClient(uri)[db_name][coll_name]


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        env_overrides = json.loads(args.env_overrides_json or "{}")
        if not isinstance(env_overrides, dict):
            raise ValueError("env-overrides-json must be a JSON object")
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"--env-overrides-json invalid: {exc}", file=sys.stderr)
        return 2

    redis_client = _connect_redis()
    mongo_coll = _connect_mongo_collection(args.source_coll)

    publisher = SimPublisher(
        run_id=args.run_id,
        source_date=args.source_date,
        source_coll=args.source_coll,
        label=args.label,
        speed=args.speed,
        max_len=args.max_len,
        redis_client=redis_client,
        mongo_collection=mongo_coll,
        image_digest=args.image_digest,
        env_overrides=env_overrides,
    )

    # Manifest first — if a run with this id already wrote one, refuse to clobber.
    try:
        manifest_path = publisher.write_initial_manifest()
        logger.info("sim manifest written path=%s", manifest_path)
    except FileExistsError as exc:
        print(f"refusing to overwrite existing manifest: {exc}", file=sys.stderr)
        return 3

    # Verify source has at least one matching snapshot before we start.
    total = publisher.count_source()
    if total == 0:
        print(
            f"no snapshots found in {args.source_coll} for trade_date_ist={args.source_date}",
            file=sys.stderr,
        )
        return 4
    logger.info(
        "sim publisher starting run_id=%s stream=%s source=%s/%s speed=%.2f bars=%d",
        publisher.run_id,
        publisher.stream_name,
        args.source_coll,
        args.source_date,
        args.speed,
        total,
    )

    def _on_signal(signum, _frame):  # noqa: ANN001
        publisher.request_stop(reason=f"signal:{signum}")

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    summary = publisher.run()
    print(json.dumps(summary, indent=2))
    return 0 if summary["terminal_status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
