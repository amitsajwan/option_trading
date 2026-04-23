from __future__ import annotations

import json
import math
import os
import re
import uuid
from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Optional

import redis
from pymongo import ASCENDING, DESCENDING, MongoClient

from contracts_app import IST_ZONE, TimestampSourceMode, isoformat_ist, parse_timestamp_to_ist

_REASON_RE = re.compile(r"^\[(?P<regime>[^\]]+)\]\s+(?P<strategy>[^:]+):")
BANKNIFTY_OPTION_LOT_SIZE = 15.0


def _parse_csv_filter(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for part in str(raw).split(","):
        text = str(part).strip()
        if text:
            out.append(text)
    return list(dict.fromkeys(out))


def _safe_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except Exception:
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _parse_reason(reason: str) -> tuple[Optional[str], Optional[str]]:
    match = _REASON_RE.match(str(reason or "").strip())
    if not match:
        return None, None
    strategy = str(match.group("strategy") or "").strip() or None
    regime = str(match.group("regime") or "").strip() or None
    return strategy, regime


def _resolve_position_signal_id(*docs: Any) -> Optional[str]:
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        text = str(doc.get("signal_id") or "").strip()
        if text:
            return text
    return None


def _iso_or_none(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return isoformat_ist(value, naive_mode=TimestampSourceMode.LEGACY_MONGO_UTC)
    text = str(value or "").strip()
    return text or None


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    return parse_timestamp_to_ist(text, naive_mode=TimestampSourceMode.MARKET_IST)


def _pagination_meta(*, total_rows: int, page: int, page_size: int) -> dict[str, Any]:
    total_pages = max(1, math.ceil(max(0, int(total_rows)) / max(1, int(page_size))))
    page_safe = min(max(1, int(page)), total_pages)
    return {
        "page": page_safe,
        "page_size": int(page_size),
        "total_rows": int(total_rows),
        "total_pages": total_pages,
    }


def _streaks_from_signs(values: list[int]) -> tuple[int, int]:
    max_win = 0
    max_loss = 0
    cur_win = 0
    cur_loss = 0
    for item in values:
        if item > 0:
            cur_win += 1
            cur_loss = 0
            max_win = max(max_win, cur_win)
        elif item < 0:
            cur_loss += 1
            cur_win = 0
            max_loss = max(max_loss, cur_loss)
        else:
            cur_win = 0
            cur_loss = 0
    return max_win, max_loss


def _safe_ratio(numerator: float, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def _resolve_trail_mechanism(*, exit_reason: Any, trailing_active: Any, orb_trail_active: Any, oi_trail_active: Any) -> Optional[str]:
    if str(exit_reason or "").strip().upper() != "TRAILING_STOP":
        return None
    if bool(orb_trail_active):
        return "ORB_TRAIL"
    if bool(oi_trail_active):
        return "OI_TRAIL"
    if bool(trailing_active):
        return "GENERIC_TRAIL"
    return "TRAILING_STOP"


class StrategyEvaluationService:
    def __init__(self) -> None:
        self._mongo_client: Optional[MongoClient] = None
        self._redis_client: Optional[redis.Redis] = None
        self._indexes_ready = False

    def _mongo(self) -> MongoClient:
        if self._mongo_client is not None:
            return self._mongo_client
        uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
        if uri:
            self._mongo_client = MongoClient(
                uri,
                serverSelectionTimeoutMS=3000,
                connectTimeoutMS=3000,
                socketTimeoutMS=5000,
            )
        else:
            self._mongo_client = MongoClient(
                host=str(os.getenv("MONGO_HOST") or "localhost"),
                port=int(os.getenv("MONGO_PORT") or "27017"),
                serverSelectionTimeoutMS=3000,
                connectTimeoutMS=3000,
                socketTimeoutMS=5000,
            )
        self._mongo_client.admin.command("ping")
        self._ensure_indexes()
        return self._mongo_client

    def _redis(self) -> redis.Redis:
        if self._redis_client is not None:
            return self._redis_client
        self._redis_client = redis.Redis(
            host=str(os.getenv("REDIS_HOST") or "localhost"),
            port=int(os.getenv("REDIS_PORT") or "6379"),
            db=int(os.getenv("REDIS_DB") or "0"),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        return self._redis_client

    def _db(self):
        mongo = self._mongo()
        db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
        return mongo[db_name]

    def _collection_names(self, dataset: str) -> dict[str, str]:
        mode = str(dataset or "historical").strip().lower()
        if mode not in {"historical", "live"}:
            raise ValueError(f"unsupported dataset '{dataset}'")
        if mode == "historical":
            return {
                "votes": str(os.getenv("MONGO_COLL_STRATEGY_VOTES_HISTORICAL") or "strategy_votes_historical"),
                "signals": str(os.getenv("MONGO_COLL_TRADE_SIGNALS_HISTORICAL") or "trade_signals_historical"),
                "positions": str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL") or "strategy_positions_historical"),
                "traces": str(
                    os.getenv("MONGO_COLL_STRATEGY_DECISION_TRACES_HISTORICAL") or "strategy_decision_traces_historical"
                ),
            }
        return {
            "votes": str(os.getenv("MONGO_COLL_STRATEGY_VOTES") or "strategy_votes"),
            "signals": str(os.getenv("MONGO_COLL_TRADE_SIGNALS") or "trade_signals"),
            "positions": str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS") or "strategy_positions"),
            "traces": str(os.getenv("MONGO_COLL_STRATEGY_DECISION_TRACES") or "strategy_decision_traces"),
        }

    def _runs_collection_name(self) -> str:
        return str(os.getenv("MONGO_COLL_STRATEGY_EVAL_RUNS") or "strategy_eval_runs")

    def _date_match(self, *, date_from: str, date_to: str, run_id: Optional[str]) -> dict[str, Any]:
        query: dict[str, Any] = {"trade_date_ist": {"$gte": str(date_from), "$lte": str(date_to)}}
        text = str(run_id or "").strip()
        if text:
            query["run_id"] = text
        return query

    def _ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        db = self._db()
        runs = db[self._runs_collection_name()]
        runs.create_index([("run_id", ASCENDING)], unique=True)
        runs.create_index([("status", ASCENDING), ("submitted_at", ASCENDING)])
        runs.create_index([("dataset", ASCENDING), ("date_from", ASCENDING), ("date_to", ASCENDING)])
        self._indexes_ready = True

    def _run_channel(self, run_id: str) -> str:
        return f"{str(os.getenv('STRATEGY_EVAL_RUN_CHANNEL_PREFIX') or 'strategy:eval:run:')}{run_id}"

    def _global_channel(self) -> str:
        return str(os.getenv("STRATEGY_EVAL_GLOBAL_CHANNEL") or "strategy:eval:global")

    def _command_channel(self) -> str:
        return str(os.getenv("STRATEGY_EVAL_COMMAND_TOPIC") or "strategy:eval:command")

    def _publish_run_event(self, run_id: str, payload: dict[str, Any]) -> None:
        body = dict(payload or {})
        body["run_id"] = run_id
        body["timestamp"] = isoformat_ist()
        rendered = json.dumps(body, ensure_ascii=False, default=str)
        redis_client = self._redis()
        redis_client.publish(self._run_channel(run_id), rendered)
        redis_client.publish(self._global_channel(), rendered)

    def queue_replay_run(
        self,
        *,
        dataset: str,
        date_from: str,
        date_to: str,
        speed: float,
        base_path: Optional[str],
        risk_config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        mode = str(dataset or "historical").strip().lower()
        if mode != "historical":
            raise ValueError("dataset must be historical for v1 replay")
        run_id = str(uuid.uuid4())
        submitted_at = isoformat_ist()
        doc = {
            "run_id": run_id,
            "status": "queued",
            "dataset": mode,
            "date_from": str(date_from),
            "date_to": str(date_to),
            "speed": float(speed),
            "base_path": str(base_path or ""),
            "risk_config": dict(risk_config or {}),
            "submitted_at": submitted_at,
            "started_at": None,
            "ended_at": None,
            "progress_pct": 0.0,
            "current_day": None,
            "total_days": None,
            "message": "Queued",
            "error": None,
            "updated_at": submitted_at,
        }
        db = self._db()
        db[self._runs_collection_name()].insert_one(doc)

        command = {
            "event_type": "strategy_eval_run_command",
            "event_version": "1.0",
            "run_id": run_id,
            "dataset": mode,
            "date_from": str(date_from),
            "date_to": str(date_to),
            "speed": float(speed),
            "base_path": str(base_path or ""),
            "risk_config": dict(risk_config or {}),
            "submitted_at": submitted_at,
        }
        self._redis().publish(self._command_channel(), json.dumps(command, ensure_ascii=False, default=str))
        self._publish_run_event(
            run_id,
            {
                "event_type": "run_queued",
                "message": f"Run queued for {date_from} to {date_to}",
                "progress_pct": 0.0,
            },
        )
        return {
            "run_id": run_id,
            "status": "queued",
            "dataset": mode,
            "date_from": str(date_from),
            "date_to": str(date_to),
            "submitted_at": submitted_at,
            "progress_pct": 0.0,
            "message": "Queued",
            "error": None,
            "risk_config": dict(risk_config or {}),
            "requested_range": {"date_from": str(date_from), "date_to": str(date_to)},
        }

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        doc = self._db()[self._runs_collection_name()].find_one({"run_id": str(run_id)}, {"_id": 0})
        if not isinstance(doc, dict):
            return None
        return doc

    def get_latest_run(self, *, dataset: str = "historical", status: str = "completed") -> Optional[dict[str, Any]]:
        mode = str(dataset or "historical").strip().lower()
        state = str(status or "completed").strip().lower()
        doc = self._db()[self._runs_collection_name()].find_one(
            {"dataset": mode, "status": state},
            {"_id": 0},
            sort=[("submitted_at", DESCENDING)],
        )
        return doc if isinstance(doc, dict) else None

    def list_runs(
        self,
        *,
        dataset: str = "historical",
        status: Optional[str] = None,
        limit: int = 20,
        include_counts: bool = True,
    ) -> dict[str, Any]:
        """Return recent evaluation runs so the UI can show a picker.

        When ``include_counts`` is true (default), the returned rows are
        enriched with ``trade_count`` / ``signal_count`` / ``vote_count``
        computed from the corresponding persistence collections, so operators
        can see at a glance whether a run actually produced trades.
        """
        mode = str(dataset or "historical").strip().lower()
        if mode not in {"historical", "live"}:
            raise ValueError(f"unsupported dataset '{dataset}'")
        capped_limit = max(1, min(int(limit or 20), 200))
        query: dict[str, Any] = {"dataset": mode}
        state = str(status or "").strip().lower()
        if state:
            query["status"] = state
        db = self._db()
        cursor = db[self._runs_collection_name()].find(
            query,
            {"_id": 0},
            sort=[("submitted_at", DESCENDING)],
        ).limit(capped_limit)
        rows: list[dict[str, Any]] = [dict(doc) for doc in cursor if isinstance(doc, dict)]

        # Discover runs written directly to data collections (e.g. tmux-launched replays
        # that never registered an eval-run record).
        if mode == "historical":
            known_ids = {str(r.get("run_id") or "").strip() for r in rows if str(r.get("run_id") or "").strip()}
            names = self._collection_names(mode)
            try:
                # Group by run_id, counting only POSITION_CLOSE events as completed trades.
                # {run_id: {$ne: ""}} matches null, missing, and non-empty string run_ids.
                discover_pipeline: list[dict[str, Any]] = [
                    {"$match": {"run_id": {"$nin": [None, ""]}}},  
                    {
                        "$group": {
                            "_id": "$run_id",
                            "min_date": {"$min": "$trade_date_ist"},
                            "max_date": {"$max": "$trade_date_ist"},
                            "count": {
                                "$sum": {"$cond": [{"$eq": ["$event", "POSITION_CLOSE"]}, 1, 0]}
                            },
                        }
                    },
                    {"$match": {"count": {"$gt": 0}}},
                    {"$sort": {"max_date": DESCENDING}},
                    {"$limit": capped_limit * 2},
                ]
                for d in db[names["positions"]].aggregate(discover_pipeline, allowDiskUse=True):
                    rid = str(d.get("_id") or "").strip()
                    if not rid or rid in known_ids:
                        continue
                    rows.append(
                        {
                            "run_id": rid,
                            "dataset": mode,
                            "status": "completed",
                            "date_from": str(d.get("min_date") or ""),
                            "date_to": str(d.get("max_date") or ""),
                            "trade_count": int(d.get("count") or 0),
                            "signal_count": 0,
                            "vote_count": 0,
                            "submitted_at": None,
                            "discovered": True,
                        }
                    )
                    known_ids.add(rid)
            except Exception:
                pass

        if include_counts and rows and mode == "historical":
            run_ids = [str(row.get("run_id") or "").strip() for row in rows if str(row.get("run_id") or "").strip()]
            if run_ids:
                names = self._collection_names(mode)
                positions_coll = db[names["positions"]]
                signals_coll = db[names["signals"]]
                votes_coll = db[names["votes"]]
                def _group_counts(coll: Any, positions_only: bool = False) -> dict[str, int]:
                    out: dict[str, int] = {}
                    try:
                        match_stage: dict[str, Any] = {"run_id": {"$in": run_ids}}
                        if positions_only:
                            match_stage["event"] = "POSITION_CLOSE"
                        for r in coll.aggregate([
                            {"$match": match_stage},
                            {"$group": {"_id": "$run_id", "n": {"$sum": 1}}},
                        ], allowDiskUse=True):
                            out[str(r.get("_id") or "")] = int(r.get("n") or 0)
                    except Exception:
                        pass
                    return out

                trade_counts = _group_counts(positions_coll, positions_only=True)
                signal_counts = _group_counts(signals_coll)
                vote_counts = _group_counts(votes_coll)
                for row in rows:
                    rid = str(row.get("run_id") or "").strip()
                    row["trade_count"] = int(trade_counts.get(rid, 0))
                    row["signal_count"] = int(signal_counts.get(rid, 0))
                    row["vote_count"] = int(vote_counts.get(rid, 0))

        # Push most-recent runs (by end date) to the top regardless of source.
        rows.sort(key=lambda r: r.get("date_to") or "", reverse=True)

        return {
            "rows": rows,
            "total": len(rows),
            "dataset": mode,
            "status": state or None,
            "limit": capped_limit,
        }

    def _resolve_run_scope(self, *, dataset: str, run_id: Optional[str]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        mode = str(dataset or "historical").strip().lower()
        if mode != "historical":
            return None, None
        requested = str(run_id or "").strip()
        if requested:
            # Registered run check first — valid for queued/running/completed states.
            registered = self.get_run(requested)
            if isinstance(registered, dict) and str(registered.get("dataset") or "").lower() == "historical":
                return requested, registered
            # Fall back to data-collection scan for tmux/unregistered runs.
            data_match: dict[str, Any] = {"run_id": requested}
            names = self._collection_names(mode)
            db = self._db()
            found = False
            for coll_name in (names["positions"], names["signals"]):
                try:
                    if db[coll_name].count_documents(data_match, limit=1):
                        found = True
                        break
                except Exception:
                    pass
            if not found:
                raise ValueError(f"run_id '{requested}' not found in data collections")
            return requested, {"run_id": requested, "dataset": mode, "status": "completed", "discovered": True}
        # No run_id specified — find the most recent run from data collections.
        names = self._collection_names(mode)
        db = self._db()
        try:
            doc = db[names["positions"]].find_one(
                {"run_id": {"$nin": [None, ""]}},
                {"run_id": 1},
                sort=[("trade_date_ist", DESCENDING)],
            )
            if isinstance(doc, dict) and doc.get("run_id"):
                resolved = str(doc["run_id"]).strip()
                return resolved, {"run_id": resolved, "dataset": mode, "status": "completed", "discovered": True}
        except Exception:
            pass
        raise ValueError("no historical replay data found")

    def _load_signal_map(
        self,
        *,
        signals_coll: Any,
        date_match: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        projection = {
            "_id": 0,
            "signal_id": 1,
            "regime": 1,
            "confidence": 1,
            "reason": 1,
            "decision_metrics": 1,
            "decision_reason_code": 1,
            "payload.signal": 1,
            "trade_date_ist": 1,
        }
        out: dict[str, dict[str, Any]] = {}
        for doc in signals_coll.find(date_match, projection):
            signal_id = str(doc.get("signal_id") or "").strip()
            if not signal_id:
                continue
            payload_signal = (
                ((doc.get("payload") or {}).get("signal")) if isinstance(doc.get("payload"), dict) else {}
            ) or {}
            if not isinstance(payload_signal, dict):
                payload_signal = {}
            decision_metrics = doc.get("decision_metrics") if isinstance(doc.get("decision_metrics"), dict) else None
            if decision_metrics is None and isinstance(payload_signal.get("decision_metrics"), dict):
                decision_metrics = dict(payload_signal.get("decision_metrics") or {})
            out[signal_id] = {
                "signal_id": signal_id,
                "regime": str(doc.get("regime") or payload_signal.get("regime") or "").strip() or None,
                "confidence": _safe_float(
                    doc.get("confidence") if doc.get("confidence") is not None else payload_signal.get("confidence")
                ),
                "reason": str(doc.get("reason") or payload_signal.get("reason") or "").strip(),
                "decision_metrics": dict(decision_metrics or {}),
                "decision_reason_code": str(
                    doc.get("decision_reason_code") or payload_signal.get("decision_reason_code") or ""
                ).strip()
                or None,
                "contributing_strategies": list(payload_signal.get("contributing_strategies") or []),
                "timestamp": _iso_or_none(payload_signal.get("timestamp") or doc.get("timestamp")),
                "trade_date_ist": str(doc.get("trade_date_ist") or "").strip() or None,
            }
        return out

    def _load_positions(
        self,
        *,
        positions_coll: Any,
        date_match: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        projection = {
            "_id": 0,
            "position_id": 1,
            "signal_id": 1,
            "event": 1,
            "timestamp": 1,
            "trade_date_ist": 1,
            "payload.position": 1,
        }
        out: dict[str, dict[str, Any]] = {}
        for doc in positions_coll.find(date_match, projection).sort("timestamp", 1):
            position_id = str(doc.get("position_id") or "").strip()
            if not position_id:
                continue
            payload_position = (
                ((doc.get("payload") or {}).get("position")) if isinstance(doc.get("payload"), dict) else {}
            ) or {}
            if not isinstance(payload_position, dict):
                payload_position = {}
            slot = out.setdefault(position_id, {"position_id": position_id})
            event = str(doc.get("event") or payload_position.get("event") or "").strip().upper()
            if event == "POSITION_OPEN":
                slot["open"] = payload_position
                slot["open_doc"] = doc
            elif event == "POSITION_CLOSE":
                slot["close"] = payload_position
                slot["close_doc"] = doc
        return out

    def _trade_from_docs(
        self,
        *,
        position_id: str,
        docs: dict[str, Any],
        signal_map: dict[str, dict[str, Any]],
        cost_bps: float,
    ) -> Optional[dict[str, Any]]:
        open_position = docs.get("open")
        close_position = docs.get("close")
        if not isinstance(open_position, dict) or not isinstance(close_position, dict):
            return None

        open_doc = docs.get("open_doc") if isinstance(docs.get("open_doc"), dict) else {}
        close_doc = docs.get("close_doc") if isinstance(docs.get("close_doc"), dict) else {}
        signal_id = str(_resolve_position_signal_id(open_position, open_doc, close_position, close_doc) or "").strip()
        signal_doc = signal_map.get(signal_id, {})
        reason_text = str(signal_doc.get("reason") or open_position.get("reason") or "")
        strategy_from_reason, regime_from_reason = _parse_reason(reason_text)
        strategy = strategy_from_reason
        if strategy is None:
            contrib = signal_doc.get("contributing_strategies")
            if isinstance(contrib, list) and contrib:
                strategy = str(contrib[0] or "").strip() or None
        regime = str(signal_doc.get("regime") or regime_from_reason or "").strip() or None

        pnl_pct = _safe_float(close_position.get("pnl_pct"))
        cost_pct = 2.0 * (float(cost_bps) / 10000.0)
        pnl_pct_net = (pnl_pct - cost_pct) if pnl_pct is not None else None
        entry_dt = _parse_iso_dt(open_position.get("timestamp"))
        exit_dt = _parse_iso_dt(close_position.get("timestamp"))
        bars_held = int(float(close_position.get("bars_held") or 0))
        trailing_active = bool(close_position.get("trailing_active")) if close_position.get("trailing_active") is not None else None
        orb_trail_active = bool(close_position.get("orb_trail_active")) if close_position.get("orb_trail_active") is not None else None
        oi_trail_active = bool(close_position.get("oi_trail_active")) if close_position.get("oi_trail_active") is not None else None
        exit_reason = str(close_position.get("exit_reason") or "").strip() or None
        result = "UNKNOWN"
        if pnl_pct_net is not None:
            if pnl_pct_net > 0:
                result = "WIN"
            elif pnl_pct_net < 0:
                result = "LOSS"
            else:
                result = "FLAT"
        return {
            "position_id": position_id,
            "signal_id": signal_id or None,
            "entry_strategy": strategy,
            "regime": regime,
            "direction": str(open_position.get("direction") or "").strip() or None,
            "strike": open_position.get("strike") if open_position.get("strike") is not None else close_position.get("strike"),
            "entry_time": _iso_or_none(open_position.get("timestamp")),
            "exit_time": _iso_or_none(close_position.get("timestamp")),
            "entry_dt": entry_dt,
            "exit_dt": exit_dt,
            "trade_date_ist": str(
                (docs.get("open_doc") or {}).get("trade_date_ist")
                or (docs.get("close_doc") or {}).get("trade_date_ist")
                or ""
            ).strip()
            or None,
            "entry_premium": _safe_float(open_position.get("entry_premium")),
            "exit_premium": _safe_float(close_position.get("exit_premium")),
            "pnl_pct": pnl_pct,
            "pnl_pct_net": pnl_pct_net,
            "mfe_pct": _safe_float(close_position.get("mfe_pct")),
            "mae_pct": _safe_float(close_position.get("mae_pct")),
            "bars_held": bars_held,
            "lots": int(float(open_position.get("lots") or 0)) if open_position.get("lots") is not None else None,
            "lot_size": (
                _safe_float(open_position.get("lot_size"))
                or _safe_float(close_position.get("lot_size"))
                or BANKNIFTY_OPTION_LOT_SIZE
            ),
            "stop_loss_pct": _safe_float(open_position.get("stop_loss_pct")),
            "entry_stop_price": _safe_float(open_position.get("stop_price")),
            "exit_stop_price": _safe_float(close_position.get("stop_price")),
            "high_water_premium": _safe_float(close_position.get("high_water_premium")),
            "target_pct": _safe_float(open_position.get("target_pct")),
            "trailing_enabled": bool(open_position.get("trailing_enabled")) if open_position.get("trailing_enabled") is not None else None,
            "trailing_activation_pct": _safe_float(open_position.get("trailing_activation_pct")),
            "trailing_offset_pct": _safe_float(open_position.get("trailing_offset_pct")),
            "trailing_lock_breakeven": (
                bool(open_position.get("trailing_lock_breakeven"))
                if open_position.get("trailing_lock_breakeven") is not None
                else None
            ),
            "trailing_active": trailing_active,
            "orb_trail_active": orb_trail_active,
            "orb_trail_stop_price": _safe_float(close_position.get("orb_trail_stop_price")),
            "oi_trail_active": oi_trail_active,
            "oi_trail_stop_price": _safe_float(close_position.get("oi_trail_stop_price")),
            "signal_confidence": _safe_float(signal_doc.get("confidence")),
            "signal_decision_metrics": (
                dict(signal_doc.get("decision_metrics") or {})
                if isinstance(signal_doc.get("decision_metrics"), dict)
                else {}
            ),
            "signal_decision_reason_code": str(signal_doc.get("decision_reason_code") or "").strip() or None,
            "exit_reason": exit_reason,
            "exit_mechanism": _resolve_trail_mechanism(
                exit_reason=exit_reason,
                trailing_active=trailing_active,
                orb_trail_active=orb_trail_active,
                oi_trail_active=oi_trail_active,
            ),
            "result": result,
            "entry_reason": str(open_position.get("reason") or "").strip() or None,
        }

    def _apply_capital_metrics(self, trades: list[dict[str, Any]], *, initial_capital: float) -> list[dict[str, Any]]:
        capital_base = float(initial_capital)
        enriched: list[dict[str, Any]] = []
        for trade in trades:
            row = dict(trade)
            pnl_pct_net = _safe_float(row.get("pnl_pct_net"))
            entry_premium = _safe_float(row.get("entry_premium"))
            lots = _safe_float(row.get("lots"))
            lot_size = _safe_float(row.get("lot_size")) or BANKNIFTY_OPTION_LOT_SIZE
            capital_at_risk = None
            capital_pnl_amount = None
            capital_pnl_pct = None
            if entry_premium is not None and entry_premium > 0 and lots is not None and lots > 0 and lot_size > 0:
                capital_at_risk = float(entry_premium) * float(lots) * float(lot_size)
            if pnl_pct_net is not None and capital_at_risk is not None:
                capital_pnl_amount = float(pnl_pct_net) * capital_at_risk
                if capital_base > 0:
                    capital_pnl_pct = capital_pnl_amount / capital_base
            row["capital_at_risk"] = capital_at_risk
            row["capital_pnl_amount"] = capital_pnl_amount
            row["capital_pnl_pct"] = capital_pnl_pct
            enriched.append(row)
        return enriched

    def _summarize_trades(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        capital_pnls = [trade["capital_pnl_pct"] for trade in trades if trade.get("capital_pnl_pct") is not None]
        raw_pnls = [trade["pnl_pct_net"] for trade in trades if trade.get("pnl_pct_net") is not None]
        mfes = [trade["mfe_pct"] for trade in trades if trade.get("mfe_pct") is not None]
        maes = [trade["mae_pct"] for trade in trades if trade.get("mae_pct") is not None]
        bars = [trade["bars_held"] for trade in trades if trade.get("bars_held") is not None]
        winners = [value for value in capital_pnls if value > 0]
        losers = [value for value in capital_pnls if value < 0]
        flats = [value for value in capital_pnls if value == 0]
        gross_profit = sum(winners)
        gross_loss = abs(sum(losers))
        profit_factor = None
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        return {
            "trades": len(trades),
            "wins": len(winners),
            "losses": len(losers),
            "flats": len(flats),
            "win_rate": (len(winners) / len(capital_pnls)) if capital_pnls else None,
            "avg_capital_pnl_pct": (sum(capital_pnls) / len(capital_pnls)) if capital_pnls else None,
            "median_capital_pnl_pct": median(capital_pnls) if capital_pnls else None,
            "avg_winner_pct": (sum(winners) / len(winners)) if winners else None,
            "avg_loser_pct": (sum(losers) / len(losers)) if losers else None,
            "gross_profit_capital_pct": gross_profit if winners else 0.0,
            "gross_loss_capital_pct": -gross_loss if losers else 0.0,
            "profit_factor": profit_factor,
            "expectancy_capital_pct": (sum(capital_pnls) / len(capital_pnls)) if capital_pnls else None,
            "avg_trade_pnl_pct": (sum(raw_pnls) / len(raw_pnls)) if raw_pnls else None,
            "median_trade_pnl_pct": median(raw_pnls) if raw_pnls else None,
            "avg_mfe_pct": (sum(mfes) / len(mfes)) if mfes else None,
            "avg_mae_pct": (sum(maes) / len(maes)) if maes else None,
            "avg_bars_held": (sum(bars) / len(bars)) if bars else None,
        }

    def _exit_reason_breakdown(self, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        total = len(trades)
        for trade in trades:
            reason = str(trade.get("exit_reason") or "UNKNOWN").strip() or "UNKNOWN"
            grouped[reason].append(trade)

        rows: list[dict[str, Any]] = []
        for reason, items in grouped.items():
            pnls = [item["pnl_pct_net"] for item in items if item.get("pnl_pct_net") is not None]
            capital_pnls = [item["capital_pnl_pct"] for item in items if item.get("capital_pnl_pct") is not None]
            rows.append(
                {
                    "exit_reason": reason,
                    "count": len(items),
                    "pct": _safe_ratio(len(items), total),
                    "avg_pnl_pct_net": (sum(pnls) / len(pnls)) if pnls else None,
                    "avg_capital_pnl_pct": (sum(capital_pnls) / len(capital_pnls)) if capital_pnls else None,
                }
            )
        rows.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("exit_reason") or "")))
        return rows

    def _stop_analysis(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(trades)
        trailing_stop_trades = [trade for trade in trades if str(trade.get("exit_reason") or "").upper() == "TRAILING_STOP"]
        hard_stop_trades = [trade for trade in trades if str(trade.get("exit_reason") or "").upper() == "STOP_LOSS"]
        generic_trailing_active_trades = [trade for trade in trades if bool(trade.get("trailing_active"))]
        orb_trailing_active_trades = [trade for trade in trades if bool(trade.get("orb_trail_active"))]
        oi_trailing_active_trades = [trade for trade in trades if bool(trade.get("oi_trail_active"))]
        trailing_active_trades = [
            trade
            for trade in trades
            if bool(trade.get("trailing_active")) or bool(trade.get("orb_trail_active")) or bool(trade.get("oi_trail_active"))
        ]
        locked_gain_pcts: list[float] = []
        trailing_captured_pcts: list[float] = []

        for trade in trailing_stop_trades:
            entry = _safe_float(trade.get("entry_premium"))
            stop = _safe_float(trade.get("exit_stop_price"))
            high_water = _safe_float(trade.get("high_water_premium"))
            if entry is not None and entry > 0 and stop is not None:
                locked_gain_pcts.append(max(0.0, (float(stop) - float(entry)) / float(entry)))
            if entry is not None and entry > 0 and stop is not None and high_water is not None and high_water > entry:
                numerator = max(0.0, float(stop) - float(entry))
                denominator = max(0.0, float(high_water) - float(entry))
                if denominator > 0:
                    trailing_captured_pcts.append(numerator / denominator)

        avg_stop_loss_pct_values = [_safe_float(trade.get("stop_loss_pct")) for trade in trades]
        avg_target_pct_values = [_safe_float(trade.get("target_pct")) for trade in trades]
        avg_stop_loss_pct = [value for value in avg_stop_loss_pct_values if value is not None]
        avg_target_pct = [value for value in avg_target_pct_values if value is not None]

        return {
            "stop_loss_exits": len(hard_stop_trades),
            "stop_loss_exit_pct": _safe_ratio(len(hard_stop_trades), total),
            "trailing_stop_exits": len(trailing_stop_trades),
            "trailing_stop_exit_pct": _safe_ratio(len(trailing_stop_trades), total),
            "trailing_active_trades": len(trailing_active_trades),
            "trailing_active_trade_pct": _safe_ratio(len(trailing_active_trades), total),
            "generic_trailing_active_trades": len(generic_trailing_active_trades),
            "generic_trailing_active_trade_pct": _safe_ratio(len(generic_trailing_active_trades), total),
            "orb_trailing_active_trades": len(orb_trailing_active_trades),
            "orb_trailing_active_trade_pct": _safe_ratio(len(orb_trailing_active_trades), total),
            "oi_trailing_active_trades": len(oi_trailing_active_trades),
            "oi_trailing_active_trade_pct": _safe_ratio(len(oi_trailing_active_trades), total),
            "avg_locked_gain_pct_before_trailing_exit": (
                sum(locked_gain_pcts) / len(locked_gain_pcts) if locked_gain_pcts else None
            ),
            "avg_trailing_profit_capture_pct": (
                sum(trailing_captured_pcts) / len(trailing_captured_pcts) if trailing_captured_pcts else None
            ),
            "avg_configured_stop_loss_pct": (
                sum(avg_stop_loss_pct) / len(avg_stop_loss_pct) if avg_stop_loss_pct else None
            ),
            "avg_configured_target_pct": (
                sum(avg_target_pct) / len(avg_target_pct) if avg_target_pct else None
            ),
        }

    def _group_breakdown(self, trades: list[dict[str, Any]], group_key: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in trades:
            key = str(trade.get(group_key) or "UNKNOWN").strip() or "UNKNOWN"
            grouped[key].append(trade)

        rows: list[dict[str, Any]] = []
        for key, items in grouped.items():
            capital_pnls = [item["capital_pnl_pct"] for item in items if item.get("capital_pnl_pct") is not None]
            raw_pnls = [item["pnl_pct_net"] for item in items if item.get("pnl_pct_net") is not None]
            winners = [value for value in capital_pnls if value > 0]
            losers = [value for value in capital_pnls if value < 0]
            gross_loss = abs(sum(losers))
            rows.append(
                {
                    group_key: key,
                    "trades": len(items),
                    "win_rate": _safe_ratio(len(winners), len(capital_pnls)) if capital_pnls else None,
                    "avg_capital_pnl_pct": (sum(capital_pnls) / len(capital_pnls)) if capital_pnls else None,
                    "total_capital_pnl_pct": sum(capital_pnls) if capital_pnls else 0.0,
                    "avg_trade_pnl_pct": (sum(raw_pnls) / len(raw_pnls)) if raw_pnls else None,
                    "profit_factor": (_safe_ratio(sum(winners), gross_loss) if gross_loss > 0 else None),
                }
            )
        rows.sort(
            key=lambda item: (
                -float(item.get("total_capital_pnl_pct") or 0.0),
                -int(item.get("trades") or 0),
                str(item.get(group_key) or ""),
            )
        )
        return rows

    def _build_equity(self, *, trades: list[dict[str, Any]], initial_capital: float) -> dict[str, Any]:
        ordered = sorted(
            [trade for trade in trades if trade.get("capital_pnl_amount") is not None],
            key=lambda item: ((item.get("exit_dt") or datetime.min.replace(tzinfo=IST_ZONE)), str(item.get("position_id") or "")),
        )
        equity = float(initial_capital)
        peak = float(initial_capital)
        max_drawdown_pct = 0.0
        trade_signs: list[int] = []
        equity_curve: list[dict[str, Any]] = []
        drawdown_curve: list[dict[str, Any]] = []
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for trade in ordered:
            capital_pnl_amount = float(trade["capital_pnl_amount"])
            capital_pnl_pct = _safe_float(trade.get("capital_pnl_pct"))
            trade_signs.append(1 if capital_pnl_amount > 0 else (-1 if capital_pnl_amount < 0 else 0))
            day = str(trade.get("trade_date_ist") or "")
            if day:
                by_day[day].append(trade)
            equity += capital_pnl_amount
            peak = max(peak, equity)
            drawdown = (equity / peak) - 1.0 if peak > 0 else 0.0
            drawdown = max(-1.0, drawdown)
            max_drawdown_pct = min(max_drawdown_pct, drawdown)
            date_key = day or (str(trade.get("exit_time") or "")[:10] if trade.get("exit_time") else "")
            equity_curve.append(
                {
                    "date": date_key,
                    "equity": equity,
                    "cumulative_return_pct": (equity / float(initial_capital)) - 1.0 if initial_capital > 0 else None,
                }
            )
            drawdown_curve.append({"date": date_key, "drawdown_pct": drawdown, "peak_equity": peak})

        day_keys = sorted(by_day.keys())
        day_rows: list[dict[str, Any]] = []
        day_signs: list[int] = []
        day_equity = float(initial_capital)
        day_peak = float(initial_capital)
        for day in day_keys:
            day_trades = sorted(
                by_day[day],
                key=lambda item: ((item.get("exit_dt") or datetime.min.replace(tzinfo=IST_ZONE)), str(item.get("position_id") or "")),
            )
            equity_start = day_equity
            day_wins = 0
            day_losses = 0
            day_best: Optional[float] = None
            day_worst: Optional[float] = None
            for trade in day_trades:
                pnl_amount = _safe_float(trade.get("capital_pnl_amount"))
                if pnl_amount is None:
                    continue
                val = _safe_float(trade.get("capital_pnl_pct"))
                day_equity += pnl_amount
                if pnl_amount > 0:
                    day_wins += 1
                elif pnl_amount < 0:
                    day_losses += 1
                if val is not None:
                    if day_best is None or val > day_best:
                        day_best = val
                    if day_worst is None or val < day_worst:
                        day_worst = val
            equity_end = day_equity
            day_pnl_amount = equity_end - equity_start
            day_return_pct = (day_pnl_amount / equity_start) if equity_start > 0 else None
            day_peak = max(day_peak, equity_end)
            drawdown_eod = (equity_end / day_peak) - 1.0 if day_peak > 0 else 0.0
            drawdown_eod = max(-1.0, drawdown_eod)
            if day_return_pct is not None:
                day_signs.append(1 if day_return_pct > 0 else (-1 if day_return_pct < 0 else 0))
            day_rows.append(
                {
                    "date": day,
                    "trades": len(day_trades),
                    "wins": day_wins,
                    "losses": day_losses,
                    "win_rate": (day_wins / len(day_trades)) if day_trades else None,
                    "day_return_pct": day_return_pct,
                    "day_pnl_amount": day_pnl_amount,
                    "equity_start": equity_start,
                    "equity_end": equity_end,
                    "drawdown_pct_eod": drawdown_eod,
                    "best_trade_pct": day_best,
                    "worst_trade_pct": day_worst,
                }
            )

        max_trade_win_streak, max_trade_loss_streak = _streaks_from_signs(trade_signs)
        max_day_win_streak, max_day_loss_streak = _streaks_from_signs(day_signs)
        return {
            "start_capital": float(initial_capital),
            "end_capital": equity,
            "net_return_pct": ((equity / float(initial_capital)) - 1.0) if initial_capital > 0 else None,
            "max_drawdown_pct": max_drawdown_pct,
            "equity_curve": equity_curve,
            "drawdown_curve": drawdown_curve,
            "daily_returns": [
                {
                    "date": row["date"],
                    "day_return_pct": row["day_return_pct"],
                    "day_pnl_amount": row["day_pnl_amount"],
                    "trades": row["trades"],
                }
                for row in day_rows
            ],
            "days": day_rows,
            "streaks": {
                "max_trade_win_streak": max_trade_win_streak,
                "max_trade_loss_streak": max_trade_loss_streak,
                "max_day_win_streak": max_day_win_streak,
                "max_day_loss_streak": max_day_loss_streak,
            },
        }

    def _query_and_reconstruct(
        self,
        *,
        dataset: str,
        date_from: str,
        date_to: str,
        strategy_filter: list[str],
        regime_filter: list[str],
        cost_bps: float,
        run_id: Optional[str],
        resolved_run: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        names = self._collection_names(dataset)
        db = self._db()
        votes = db[names["votes"]]
        signals = db[names["signals"]]
        positions = db[names["positions"]]
        date_match = self._date_match(date_from=date_from, date_to=date_to, run_id=run_id)

        votes_query = dict(date_match)
        if strategy_filter:
            votes_query["strategy"] = {"$in": [str(x).upper() for x in strategy_filter]}
        if regime_filter:
            votes_query["regime"] = {"$in": regime_filter}
        signals_query = dict(date_match)
        if regime_filter:
            signals_query["regime"] = {"$in": regime_filter}

        signal_map = self._load_signal_map(signals_coll=signals, date_match=date_match)
        position_map = self._load_positions(positions_coll=positions, date_match=date_match)
        raw_trades = [
            trade
            for position_id, docs in position_map.items()
            for trade in [self._trade_from_docs(position_id=position_id, docs=docs, signal_map=signal_map, cost_bps=cost_bps)]
            if trade is not None
        ]
        strategy_lookup = {s.upper() for s in strategy_filter}
        regime_lookup = set(regime_filter)
        filtered_trades = []
        for trade in raw_trades:
            if strategy_lookup:
                candidate = str(trade.get("entry_strategy") or "").upper()
                if candidate not in strategy_lookup:
                    continue
            if regime_lookup:
                candidate_regime = str(trade.get("regime") or "")
                if candidate_regime not in regime_lookup:
                    continue
            filtered_trades.append(trade)
        filtered_trades.sort(
            key=lambda item: ((item.get("exit_dt") or datetime.min.replace(tzinfo=IST_ZONE)), str(item.get("position_id") or ""))
        )
        incomplete = [
            {
                "position_id": position_id,
                "has_open": isinstance(docs.get("open"), dict),
                "has_close": isinstance(docs.get("close"), dict),
            }
            for position_id, docs in sorted(position_map.items())
            if not (isinstance(docs.get("open"), dict) and isinstance(docs.get("close"), dict))
        ]
        return {
            "counts": {
                "votes": votes.count_documents(votes_query),
                "signals": signals.count_documents(signals_query),
                "position_events": positions.count_documents(date_match),
                "closed_trades": len(filtered_trades),
                "incomplete_positions": len(incomplete),
            },
            "trades": filtered_trades,
            "incomplete_positions": incomplete,
            "resolved_run_id": str(run_id or "").strip() or None,
            "resolved_run_range": (
                {
                    "date_from": str((resolved_run or {}).get("date_from") or ""),
                    "date_to": str((resolved_run or {}).get("date_to") or ""),
                }
                if isinstance(resolved_run, dict)
                else None
            ),
        }

    def compute_summary(
        self,
        *,
        dataset: str,
        date_from: str,
        date_to: str,
        strategies: list[str],
        regimes: list[str],
        initial_capital: float,
        cost_bps: float,
        run_id: Optional[str],
    ) -> dict[str, Any]:
        resolved_run_id, resolved_run = self._resolve_run_scope(dataset=dataset, run_id=run_id)
        base = self._query_and_reconstruct(
            dataset=dataset,
            date_from=date_from,
            date_to=date_to,
            strategy_filter=strategies,
            regime_filter=regimes,
            cost_bps=cost_bps,
            run_id=resolved_run_id,
            resolved_run=resolved_run,
        )
        trades = self._apply_capital_metrics(base["trades"], initial_capital=float(initial_capital))
        summary = self._summarize_trades(trades)
        equity = self._build_equity(trades=trades, initial_capital=float(initial_capital))
        return {
            "status": "ok",
            "filters": {
                "dataset": dataset,
                "date_from": date_from,
                "date_to": date_to,
                "strategy": strategies,
                "regime": regimes,
                "initial_capital": float(initial_capital),
                "cost_bps": float(cost_bps),
                "run_id": resolved_run_id,
            },
            "counts": base["counts"],
            "resolved_run_id": base["resolved_run_id"],
            "resolved_run_range": base["resolved_run_range"],
            "overall": summary,
            "exit_reasons": self._exit_reason_breakdown(trades),
            "stop_analysis": self._stop_analysis(trades),
            "by_strategy": self._group_breakdown(trades, "entry_strategy"),
            "by_regime": self._group_breakdown(trades, "regime"),
            "equity": {
                "start_capital": equity["start_capital"],
                "end_capital": equity["end_capital"],
                "net_return_pct": equity["net_return_pct"],
                "max_drawdown_pct": equity["max_drawdown_pct"],
            },
            "streaks": equity["streaks"],
        }

    def compute_equity(
        self,
        *,
        dataset: str,
        date_from: str,
        date_to: str,
        strategies: list[str],
        regimes: list[str],
        initial_capital: float,
        cost_bps: float,
        run_id: Optional[str],
    ) -> dict[str, Any]:
        resolved_run_id, resolved_run = self._resolve_run_scope(dataset=dataset, run_id=run_id)
        base = self._query_and_reconstruct(
            dataset=dataset,
            date_from=date_from,
            date_to=date_to,
            strategy_filter=strategies,
            regime_filter=regimes,
            cost_bps=cost_bps,
            run_id=resolved_run_id,
            resolved_run=resolved_run,
        )
        trades = self._apply_capital_metrics(base["trades"], initial_capital=float(initial_capital))
        equity = self._build_equity(trades=trades, initial_capital=float(initial_capital))
        return {
            "status": "ok",
            "filters": {
                "dataset": dataset,
                "date_from": date_from,
                "date_to": date_to,
                "strategy": strategies,
                "regime": regimes,
                "initial_capital": float(initial_capital),
                "cost_bps": float(cost_bps),
                "run_id": resolved_run_id,
            },
            "counts": base["counts"],
            "resolved_run_id": base["resolved_run_id"],
            "resolved_run_range": base["resolved_run_range"],
            "equity_curve": equity["equity_curve"],
            "drawdown_curve": equity["drawdown_curve"],
            "daily_returns": equity["daily_returns"],
        }

    def compute_days(
        self,
        *,
        dataset: str,
        date_from: str,
        date_to: str,
        strategies: list[str],
        regimes: list[str],
        initial_capital: float,
        cost_bps: float,
        page: int,
        page_size: int,
        run_id: Optional[str],
    ) -> dict[str, Any]:
        resolved_run_id, resolved_run = self._resolve_run_scope(dataset=dataset, run_id=run_id)
        base = self._query_and_reconstruct(
            dataset=dataset,
            date_from=date_from,
            date_to=date_to,
            strategy_filter=strategies,
            regime_filter=regimes,
            cost_bps=cost_bps,
            run_id=resolved_run_id,
            resolved_run=resolved_run,
        )
        trades = self._apply_capital_metrics(base["trades"], initial_capital=float(initial_capital))
        equity = self._build_equity(trades=trades, initial_capital=float(initial_capital))
        rows = list(equity["days"])
        rows.sort(key=lambda item: str(item.get("date") or ""))
        meta = _pagination_meta(total_rows=len(rows), page=page, page_size=page_size)
        start = (meta["page"] - 1) * meta["page_size"]
        end = start + meta["page_size"]
        return {
            "status": "ok",
            "filters": {
                "dataset": dataset,
                "date_from": date_from,
                "date_to": date_to,
                "strategy": strategies,
                "regime": regimes,
                "initial_capital": float(initial_capital),
                "cost_bps": float(cost_bps),
                "run_id": resolved_run_id,
            },
            "counts": base["counts"],
            "resolved_run_id": base["resolved_run_id"],
            "resolved_run_range": base["resolved_run_range"],
            "pagination": meta,
            "rows": rows[start:end],
        }

    def compute_trades(
        self,
        *,
        dataset: str,
        date_from: str,
        date_to: str,
        strategies: list[str],
        regimes: list[str],
        initial_capital: float,
        cost_bps: float,
        page: int,
        page_size: int,
        sort_by: str,
        sort_dir: str,
        run_id: Optional[str],
    ) -> dict[str, Any]:
        resolved_run_id, resolved_run = self._resolve_run_scope(dataset=dataset, run_id=run_id)
        base = self._query_and_reconstruct(
            dataset=dataset,
            date_from=date_from,
            date_to=date_to,
            strategy_filter=strategies,
            regime_filter=regimes,
            cost_bps=cost_bps,
            run_id=resolved_run_id,
            resolved_run=resolved_run,
        )
        rows = self._apply_capital_metrics(base["trades"], initial_capital=float(initial_capital))
        allowed_sort = {
            "entry_time": lambda item: (item.get("entry_dt") or datetime.min.replace(tzinfo=IST_ZONE)),
            "exit_time": lambda item: (item.get("exit_dt") or datetime.min.replace(tzinfo=IST_ZONE)),
            "pnl_pct": lambda item: (_safe_float(item.get("capital_pnl_pct")) or 0.0),
        }
        key_fn = allowed_sort.get(sort_by, allowed_sort["exit_time"])
        reverse = str(sort_dir or "desc").lower() == "desc"
        rows.sort(key=key_fn, reverse=reverse)
        meta = _pagination_meta(total_rows=len(rows), page=page, page_size=page_size)
        start = (meta["page"] - 1) * meta["page_size"]
        end = start + meta["page_size"]
        return {
            "status": "ok",
            "filters": {
                "dataset": dataset,
                "date_from": date_from,
                "date_to": date_to,
                "strategy": strategies,
                "regime": regimes,
                "initial_capital": float(initial_capital),
                "cost_bps": float(cost_bps),
                "run_id": resolved_run_id,
            },
            "counts": base["counts"],
            "resolved_run_id": base["resolved_run_id"],
            "resolved_run_range": base["resolved_run_range"],
            "pagination": meta,
            "rows": rows[start:end],
        }

    def parse_filters(
        self,
        *,
        dataset: str,
        date_from: str,
        date_to: str,
        strategy_raw: Optional[str],
        regime_raw: Optional[str],
        run_id_raw: Optional[str],
        initial_capital: float,
        cost_bps: float,
        page: int,
        page_size: int,
        sort_by: str,
        sort_dir: str,
    ) -> dict[str, Any]:
        mode = str(dataset or "historical").strip().lower()
        if mode not in {"historical", "live"}:
            raise ValueError("dataset must be one of: historical, live")
        if not str(date_from or "").strip() or not str(date_to or "").strip():
            raise ValueError("date_from and date_to are required")
        try:
            datetime.strptime(str(date_from), "%Y-%m-%d")
            datetime.strptime(str(date_to), "%Y-%m-%d")
        except Exception as exc:
            raise ValueError("date_from/date_to must be YYYY-MM-DD") from exc
        if str(date_from) > str(date_to):
            raise ValueError("date_from must be <= date_to")
        strategies = _parse_csv_filter(strategy_raw)
        regimes = _parse_csv_filter(regime_raw)
        init_cap = float(initial_capital)
        if init_cap <= 0:
            raise ValueError("initial_capital must be > 0")
        cost = float(cost_bps)
        if cost < 0:
            raise ValueError("cost_bps must be >= 0")
        size = max(1, min(500, int(page_size)))
        page_no = max(1, int(page))
        sort_name = str(sort_by or "exit_time").strip()
        sort_direction = str(sort_dir or "desc").strip().lower()
        if sort_direction not in {"asc", "desc"}:
            sort_direction = "desc"
        run_id = str(run_id_raw or "").strip() or None
        return {
            "dataset": mode,
            "date_from": str(date_from),
            "date_to": str(date_to),
            "strategies": strategies,
            "regimes": regimes,
            "run_id": run_id,
            "initial_capital": init_cap,
            "cost_bps": cost,
            "page": page_no,
            "page_size": size,
            "sort_by": sort_name,
            "sort_dir": sort_direction,
        }
