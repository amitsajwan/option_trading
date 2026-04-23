from __future__ import annotations

import json
import re
from typing import Any, Optional

import redis

from contracts_app import historical_snapshot_topic, redis_connection_kwargs

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

    def _latest_completed_run(self) -> Optional[dict[str, Any]]:
        service = getattr(self, "_evaluation_service", None)
        if service is None or not hasattr(service, "get_latest_run"):
            return None
        try:
            item = service.get_latest_run(dataset="historical", status="completed")
        except Exception:
            return None
        return item if isinstance(item, dict) else None

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
        latest_run = self._latest_completed_run()
        if isinstance(latest_run, dict):
            parsed = _parse_date_yyyy_mm_dd(latest_run.get("date_to"))
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
        latest_run = self._latest_completed_run()
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
        if replay_status in {"", "idle", "ready", "unavailable"} and isinstance(latest_run, dict):
            start_date = latest_run.get("date_from") or start_date
            end_date = latest_run.get("date_to") or end_date
            started_at = latest_run.get("started_at") or started_at
            finished_at = latest_run.get("ended_at") or finished_at
            events_emitted = max(events_emitted, self._events_emitted_from_message(latest_run.get("message")))
            completed = str(latest_run.get("status") or "").strip().lower() == "completed"
            if completed:
                replay_status = "completed"
        # active_run_id: the UUID injected by replay_runner into Redis status.
        # This is the canonical run_id for the current (or most recent) replay.
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
            "latest_completed_run_id": (latest_run or {}).get("run_id") if isinstance(latest_run, dict) else None,
        }

    def get_historical_strategy_session(self, **kwargs: Any) -> dict[str, Any]:
        payload = self.get_strategy_session(**kwargs)
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        instrument = session.get("instrument") if isinstance(session, dict) else None
        date_ist = session.get("date_ist") if isinstance(session, dict) else None
        run_id = str(kwargs.get("run_id") or "").strip() or None
        payload["replay_status"] = self.get_replay_status(date=date_ist, instrument=instrument)
        payload["latest_completed_run"] = self._latest_completed_run()
        payload["active_run_id"] = run_id
        payload["mode"] = "historical"
        return payload


__all__ = ["HistoricalReplayMonitorService"]
