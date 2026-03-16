from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from contracts_app import (
    isoformat_ist,
    parse_timestamp_to_ist,
    find_matching_python_processes,
    is_market_open_ist,
    is_trading_day_ist,
    load_holidays,
)
from .time_utils import IST

try:
    from pymongo import MongoClient
except Exception:  # pragma: no cover
    MongoClient = None

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def _ist_now_iso() -> str:
    return isoformat_ist(datetime.now(tz=IST))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _zone_or_ist(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _load_env() -> None:
    if load_dotenv is None:
        return
    runtime_file = Path(__file__).resolve()
    repo_root = next(
        (parent for parent in runtime_file.parents if (parent / "market_data" / "src").exists()),
        runtime_file.parents[1],
    )
    candidates = [
        Path.cwd() / ".env",
        repo_root / ".env",
        repo_root / "market_data" / ".env",
    ]
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            load_dotenv(path, override=False)


def _mongo_latest() -> tuple[bool, dict[str, Any], Optional[str]]:
    if MongoClient is None:
        return False, {}, "pymongo_not_installed"

    _load_env()
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    coll_names = {
        "strategy_votes": str(os.getenv("MONGO_COLL_STRATEGY_VOTES") or "strategy_votes").strip() or "strategy_votes",
        "trade_signals": str(os.getenv("MONGO_COLL_TRADE_SIGNALS") or "trade_signals").strip() or "trade_signals",
        "strategy_positions": str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS") or "strategy_positions").strip() or "strategy_positions",
    }
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    try:
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
        client.admin.command("ping")
        db = client[db_name]
        projection = {
            "_id": 0,
            "event_type": 1,
            "event": 1,
            "signal_id": 1,
            "position_id": 1,
            "snapshot_id": 1,
            "strategy": 1,
            "regime": 1,
            "trade_date_ist": 1,
            "market_time_ist": 1,
            "timestamp": 1,
        }
        latest_doc = None
        latest_collection = None
        latest_ts: Optional[datetime] = None
        collection_counts: dict[str, int] = {}
        for label, coll_name in coll_names.items():
            coll = db[coll_name]
            count = int(coll.count_documents({}))
            collection_counts[label] = count
            doc = coll.find_one({}, projection, sort=[("timestamp", -1)])
            if not isinstance(doc, dict):
                continue
            doc_ts = doc.get("timestamp")
            if not isinstance(doc_ts, datetime):
                continue
            if latest_ts is None or doc_ts > latest_ts:
                latest_ts = doc_ts
                latest_doc = doc
                latest_collection = coll_name
        return True, {
            "db": db_name,
            "collection": latest_collection,
            "collections": coll_names,
            "counts": collection_counts,
            "total_docs": sum(collection_counts.values()),
            "latest": latest_doc,
        }, None
    except Exception as exc:
        return False, {"db": db_name, "collections": coll_names}, str(exc)


def evaluate(*, max_age_seconds: float) -> tuple[dict[str, Any], int]:
    process_matches = find_matching_python_processes(["persistence_app.main_strategy_consumer"])
    process_running = len(process_matches) > 0
    mongo_ok, mongo_data, mongo_error = _mongo_latest()
    market_session_enabled = _truthy(os.getenv("MARKET_SESSION_ENABLED", "0"))
    market_timezone = str(os.getenv("MARKET_TIMEZONE") or "Asia/Kolkata").strip() or "Asia/Kolkata"
    market_open_time = str(os.getenv("MARKET_OPEN_TIME") or "09:15").strip() or "09:15"
    market_close_time = str(os.getenv("MARKET_CLOSE_TIME") or "15:30").strip() or "15:30"
    holidays_file = str(os.getenv("NSE_HOLIDAYS_FILE") or "").strip()

    market_open = None
    trading_day = None
    if market_session_enabled:
        now_ist = datetime.now(tz=_zone_or_ist(market_timezone))
        holidays = load_holidays(holidays_file)
        market_open = is_market_open_ist(now_ist, market_open_time, market_close_time, holidays)
        trading_day = is_trading_day_ist(now_ist, holidays)

    age_seconds = None
    latest = mongo_data.get("latest") if isinstance(mongo_data, dict) else None
    if isinstance(latest, dict):
        trade_date_ist = str(latest.get("trade_date_ist") or "").strip()
        market_time_ist = str(latest.get("market_time_ist") or "").strip()
        dt_from_market = None
        if trade_date_ist and market_time_ist:
            try:
                dt_from_market = datetime.fromisoformat(f"{trade_date_ist}T{market_time_ist}").replace(tzinfo=IST)
            except Exception:
                dt_from_market = None
        if isinstance(dt_from_market, datetime):
            age_seconds = (datetime.now(tz=IST) - dt_from_market.astimezone(IST)).total_seconds()
        else:
            ts = latest.get("timestamp")
            dt = parse_timestamp_to_ist(ts)
            if dt is not None:
                age_seconds = (datetime.now(tz=IST) - dt.astimezone(IST)).total_seconds()

    status = "healthy"
    code = 0
    if not mongo_ok:
        status = "unhealthy"
        code = 2
    elif not market_session_enabled:
        total_docs = int(mongo_data.get("total_docs") or 0) if isinstance(mongo_data, dict) else 0
        if not process_running:
            status = "degraded" if total_docs > 0 else "unhealthy"
            code = 1 if total_docs > 0 else 2
        elif latest is None:
            status = "degraded"
            code = 1
        else:
            status = "healthy"
            code = 0
    elif market_session_enabled and market_open is False:
        status = "healthy" if process_running else "degraded"
        code = 0 if process_running else 1
    elif latest is None:
        status = "degraded" if process_running else "unhealthy"
        code = 1 if process_running else 2
    elif age_seconds is not None and age_seconds > float(max_age_seconds):
        status = "degraded" if process_running else "unhealthy"
        code = 1 if process_running else 2
    elif not process_running:
        status = "degraded"
        code = 1

    result = {
        "component": "strategy_persistence_app",
        "checked_at_ist": _ist_now_iso(),
        "status": status,
        "process": {
            "running": process_running,
            "count": len(process_matches),
            "pids": [int(pid) for pid, _ in process_matches[:10]],
        },
        "mongo": {
            "ok": mongo_ok,
            "error": mongo_error,
            "db": mongo_data.get("db") if isinstance(mongo_data, dict) else None,
            "collection": mongo_data.get("collection") if isinstance(mongo_data, dict) else None,
            "collections": mongo_data.get("collections") if isinstance(mongo_data, dict) else None,
            "counts": mongo_data.get("counts") if isinstance(mongo_data, dict) else None,
            "total_docs": mongo_data.get("total_docs") if isinstance(mongo_data, dict) else None,
            "latest": latest,
            "latest_age_seconds": round(float(age_seconds), 3) if age_seconds is not None else None,
        },
        "session_gate": {
            "enabled": market_session_enabled,
            "market_open": market_open,
            "trading_day": trading_day,
            "timezone": market_timezone if market_session_enabled else None,
            "open_time": market_open_time if market_session_enabled else None,
            "close_time": market_close_time if market_session_enabled else None,
            "holidays_file": holidays_file or None,
        },
    }
    return result, code


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Health check for strategy_persistence_app")
    parser.add_argument("--max-age-seconds", type=float, default=300.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result, code = evaluate(max_age_seconds=float(args.max_age_seconds))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(run_cli())
