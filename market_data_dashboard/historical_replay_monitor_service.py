from __future__ import annotations

import glob as _glob
import json
import logging
import os
import re
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import redis

from contracts_app import historical_snapshot_topic, redis_connection_kwargs

logger = logging.getLogger(__name__)

try:
    from .historical_replay_repository import HistoricalReplayRepository
    from .live_strategy_monitor_service import LiveStrategyMonitorService, _parse_date_yyyy_mm_dd
except ImportError:
    from historical_replay_repository import HistoricalReplayRepository  # type: ignore
    from live_strategy_monitor_service import LiveStrategyMonitorService, _parse_date_yyyy_mm_dd  # type: ignore


class HistoricalReplayMonitorService(LiveStrategyMonitorService):
    REPLAY_STATUS_KEY = "system:historical:replay_status"
    HISTORICAL_READY_KEY = "system:historical:data_ready"
    VIRTUAL_TIME_ENABLED_KEY = "system:virtual_time:enabled"
    VIRTUAL_TIME_CURRENT_KEY = "system:virtual_time:current"

    def __init__(self, evaluation_service: Optional[Any] = None) -> None:
        super().__init__(
            evaluation_service,
            dataset="historical",
            snapshot_collection_env="MONGO_COLL_SNAPSHOTS_HISTORICAL",
            default_snapshot_collection="phase1_market_snapshots_historical",
        )
        self._repo = HistoricalReplayRepository(self._evaluation_service)

    def _redis_client(self) -> redis.Redis:
        return redis.Redis(**redis_connection_kwargs(decode_responses=True))

    @staticmethod
    def _events_emitted_from_message(message: Any) -> int:
        text = str(message or "").strip()
        match = re.search(r"emitted=(\d+)", text)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except Exception:
            return 0

    def _read_replay_status(self) -> dict[str, Any]:
        try:
            client = self._redis_client()
            raw_status = client.get(self.REPLAY_STATUS_KEY)
            data_ready = bool(client.get(self.HISTORICAL_READY_KEY))
            virtual_time_enabled = bool(client.get(self.VIRTUAL_TIME_ENABLED_KEY))
            virtual_time_current = client.get(self.VIRTUAL_TIME_CURRENT_KEY)
        except Exception as exc:
            return {
                "status": "unavailable",
                "topic": historical_snapshot_topic(),
                "data_ready": False,
                "virtual_time_enabled": False,
                "virtual_time_current": None,
                "error": str(exc),
            }

        payload: dict[str, Any] = {}
        if isinstance(raw_status, str) and raw_status.strip():
            try:
                loaded = json.loads(raw_status)
                if isinstance(loaded, dict):
                    payload = loaded
            except Exception:
                payload = {}

        payload["topic"] = str(payload.get("topic") or historical_snapshot_topic()).strip() or historical_snapshot_topic()
        payload["data_ready"] = bool(payload.get("data_ready") or data_ready)
        payload["virtual_time_enabled"] = bool(payload.get("virtual_time_enabled") or virtual_time_enabled)
        payload["virtual_time_current"] = str(payload.get("virtual_time_current") or virtual_time_current or "").strip() or None
        payload["status"] = str(payload.get("status") or ("ready" if payload["data_ready"] else "idle")).strip().lower()
        return payload

    def get_default_replay_date(self) -> Optional[str]:
        replay = self._read_replay_status()
        for key in ("current_trade_date", "start_date", "end_date"):
            parsed = _parse_date_yyyy_mm_dd(replay.get(key))
            if parsed:
                return parsed
        return self._repo.latest_trade_date()

    def get_session_date_ist(self, date_override: Optional[str] = None) -> str:
        parsed = _parse_date_yyyy_mm_dd(date_override)
        if parsed:
            return parsed
        default_date = self.get_default_replay_date()
        if default_date:
            return default_date
        return super().get_session_date_ist(date_override)

    def get_replay_status(self, *, date: Optional[str] = None, instrument: Optional[str] = None) -> dict[str, Any]:
        date_ist = self.get_session_date_ist(date)
        replay = self._read_replay_status()
        coll_map = self._repo.collections()
        counts = {
            "votes": int(coll_map["votes"].count_documents({"trade_date_ist": date_ist})),
            "signals": int(coll_map["signals"].count_documents({"trade_date_ist": date_ist})),
            "positions": int(coll_map["positions"].count_documents({"trade_date_ist": date_ist})),
        }
        latest_snapshot = None
        query: dict[str, Any] = {"trade_date_ist": str(date_ist)}
        if instrument:
            query["instrument"] = str(instrument)
        doc = self._repo.snapshot_collection().find_one(query, {"_id": 0, "timestamp": 1}, sort=[("timestamp", -1)])
        if isinstance(doc, dict):
            latest_snapshot = doc.get("timestamp")

        replay_status = str(replay.get("status") or "").strip().lower()
        current_replay_timestamp = (
            replay.get("current_replay_timestamp")
            or replay.get("virtual_time_current")
            or latest_snapshot
        )
        start_date = replay.get("start_date") or date_ist
        end_date = replay.get("end_date") or date_ist
        events_emitted = int(replay.get("events_emitted") or 0)
        started_at = replay.get("started_at")
        finished_at = replay.get("finished_at")
        completed = replay_status in {"complete", "completed", "no_snapshots"}
        active_run_id = str(replay.get("run_id") or "").strip() or None
        return {
            "mode": "historical",
            "dataset": "historical",
            "topic": replay.get("topic") or historical_snapshot_topic(),
            "date_ist": date_ist,
            "start_date": start_date,
            "end_date": end_date,
            "speed": replay.get("speed"),
            "events_emitted": events_emitted,
            "cycles": int(replay.get("cycles") or 0),
            "current_replay_timestamp": current_replay_timestamp,
            "current_trade_date": replay.get("current_trade_date") or date_ist,
            "virtual_time_enabled": bool(replay.get("virtual_time_enabled")),
            "virtual_time_current": replay.get("virtual_time_current"),
            "data_ready": bool(replay.get("data_ready")) or completed or sum(counts.values()) > 0,
            "completed": completed,
            "status": replay_status or ("ready" if replay.get("data_ready") else "idle"),
            "started_at": started_at,
            "finished_at": finished_at,
            "latest_snapshot_timestamp": latest_snapshot,
            "collection_counts": counts,
            "active_run_id": active_run_id,
        }

    def get_replay_status_fast(self) -> dict[str, Any]:
        """Redis-only read — no Mongo queries. Used by the SSE stream endpoint."""
        replay = self._read_replay_status()
        replay_status = str(replay.get("status") or "").strip().lower()
        active_run_id = str(replay.get("run_id") or "").strip() or None
        completed = replay_status in {"complete", "completed", "no_snapshots"}
        return {
            "mode": "historical",
            "status": replay_status or ("ready" if replay.get("data_ready") else "idle"),
            "active_run_id": active_run_id,
            "run_id": active_run_id,
            "events_emitted": int(replay.get("events_emitted") or 0),
            "cycles": int(replay.get("cycles") or 0),
            "current_replay_timestamp": (
                replay.get("current_replay_timestamp") or replay.get("virtual_time_current")
            ),
            "current_trade_date": replay.get("current_trade_date"),
            "speed": replay.get("speed"),
            "start_date": replay.get("start_date"),
            "end_date": replay.get("end_date"),
            "completed": completed,
            "collection_counts": {},
        }

    @staticmethod
    def _load_chart_from_parquet(date_ist: str, instrument: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Load per-minute OHLC from Parquet futures dataset via pyarrow.

        Returns a session_chart dict in the same format as load_session_underlying_chart,
        or None if pyarrow / the dataset is unavailable.
        """
        try:
            import pyarrow.parquet as pq  # noqa: PLC0415
        except ImportError:
            return None

        parquet_base = Path(os.getenv("HISTORICAL_PARQUET_BASE", "/app/.data/ml_pipeline/parquet_data"))
        futures_root = parquet_base / "futures"
        if not futures_root.exists():
            return None

        year = str(date_ist)[:4]
        year_dir = futures_root / f"year={year}"
        search_root = year_dir if year_dir.exists() else futures_root
        parquet_files = sorted(_glob.glob(str(search_root / "**" / "*.parquet"), recursive=True))
        if not parquet_files:
            return None

        try:
            table = pq.read_table(
                parquet_files,
                columns=["timestamp", "trade_date", "symbol", "open", "high", "low", "close", "volume"],
                filters=[("trade_date", "=", date_ist)],
            )
        except Exception:
            logger.exception("historical replay: failed to read futures parquet for %s", date_ist)
            return None

        if len(table) == 0:
            return None

        table = table.sort_by([("timestamp", "ascending")])
        df = table.to_pandas()

        import pandas as pd  # noqa: PLC0415

        def _fval(v: Any, default: float) -> float:
            try:
                if v is None or pd.isna(v):
                    return default
                return float(v)
            except Exception:
                return default

        IST = timezone(timedelta(hours=5, minutes=30))
        timestamps: list[str] = []
        labels: list[str] = []
        opens_list: list[float] = []
        highs_list: list[float] = []
        lows_list: list[float] = []
        closes_list: list[float] = []
        volumes_list: list[float] = []
        resolved_instrument: Optional[str] = None

        for _, row in df.iterrows():
            ts = row.get("timestamp")
            if ts is None or (not isinstance(ts, str) and pd.isna(ts)):
                continue

            close_val = row.get("close")
            if close_val is None or pd.isna(close_val):
                continue
            close_f = float(close_val)

            try:
                ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                if ts_dt.tzinfo is None:
                    ts_ist = ts_dt.replace(tzinfo=IST)
                else:
                    ts_ist = ts_dt.astimezone(IST)
                ts_str = ts_ist.isoformat()
                label = ts_ist.strftime("%H:%M")
            except Exception:
                ts_str = str(ts)
                label = ""

            timestamps.append(ts_str)
            labels.append(label)
            opens_list.append(_fval(row.get("open"), close_f))
            highs_list.append(_fval(row.get("high"), close_f))
            lows_list.append(_fval(row.get("low"), close_f))
            closes_list.append(close_f)
            volumes_list.append(_fval(row.get("volume"), 0.0))

            if resolved_instrument is None:
                sym = str(row.get("symbol") or "").strip()
                if sym:
                    resolved_instrument = sym

        if not timestamps:
            return None

        return {
            "timestamps": timestamps,
            "labels": labels,
            "opens": opens_list,
            "highs": highs_list,
            "lows": lows_list,
            "closes": closes_list,
            "volumes": volumes_list,
            "prices": closes_list,
            "instrument": resolved_instrument or instrument,
            "source": "parquet_futures",
        }

    def load_session_underlying_chart(
        self, *, date_ist: str, instrument: Optional[str]
    ) -> Optional[dict[str, Any]]:
        """Return per-minute OHLC for historical replay, preferring Parquet over MongoDB."""
        chart = self._load_chart_from_parquet(date_ist=date_ist, instrument=instrument)
        if chart is not None:
            return chart
        return super().load_session_underlying_chart(date_ist=date_ist, instrument=instrument)

    def _resolve_latest_run_id_for_date(self, date_ist: Optional[str]) -> Optional[str]:
        """Return the run_id of the most recently submitted completed eval run for date_ist."""
        if not date_ist:
            return None
        try:
            db = self._repo._evaluation_service._db()
            runs_coll_name = str(os.getenv("MONGO_COLL_STRATEGY_EVAL_RUNS") or "strategy_eval_runs")
            doc = db[runs_coll_name].find_one(
                {
                    "status": "completed",
                    "date_from": {"$lte": str(date_ist)},
                    "date_to": {"$gte": str(date_ist)},
                },
                {"run_id": 1},
                sort=[("_id", -1)],
            )
            if doc:
                return str(doc["run_id"]).strip() or None
        except Exception:
            pass
        return None

    def get_historical_strategy_session(self, **kwargs: Any) -> dict[str, Any]:
        run_id = str(kwargs.get("run_id") or "").strip() or None
        date_arg = str(kwargs.get("date") or "").strip() or None
        if not run_id:
            # Always show the most recently submitted run — never leak stale data from an older run.
            run_id = self._resolve_latest_run_id_for_date(date_arg)
            if run_id:
                kwargs = {**kwargs, "run_id": run_id}
        try:
            payload = self.get_strategy_session(**kwargs)
        except TypeError:
            # Defensive fallback for older images that predate run-scoped
            # historical sessions.
            session_kwargs = {k: v for k, v in kwargs.items() if k != "run_id"}
            payload = self.get_strategy_session(**session_kwargs)
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        instrument = session.get("instrument") if isinstance(session, dict) else None
        date_ist = session.get("date_ist") if isinstance(session, dict) else None
        payload["replay_status"] = self.get_replay_status(date=date_ist, instrument=instrument)
        payload["active_run_id"] = run_id
        payload["mode"] = "historical"
        return payload


__all__ = ["HistoricalReplayMonitorService"]
