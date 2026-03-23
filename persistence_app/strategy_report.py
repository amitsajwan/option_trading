from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any, Iterable, Optional

from contracts_app import isoformat_ist
try:
    from pymongo import MongoClient
except Exception:  # pragma: no cover
    MongoClient = None


def _mongo_client() -> MongoClient:
    if MongoClient is None:
        raise RuntimeError("pymongo_not_installed")
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        return MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000)
    return MongoClient(
        host=str(os.getenv("MONGO_HOST") or "localhost"),
        port=int(os.getenv("MONGO_PORT") or "27017"),
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        socketTimeoutMS=5000,
    )


def _date_filter(*, date_from: Optional[str], date_to: Optional[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if date_from:
        values["$gte"] = str(date_from)
    if date_to:
        values["$lte"] = str(date_to)
    return {"trade_date_ist": values} if values else {}


def _aggregate_to_list(cursor: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in cursor]


def _latest_docs(coll: Any, *, limit: int, date_match: dict[str, Any]) -> list[dict[str, Any]]:
    projection = {
        "_id": 0,
        "signal_id": 1,
        "snapshot_id": 1,
        "strategy": 1,
        "regime": 1,
        "signal_type": 1,
        "direction": 1,
        "confidence": 1,
        "timestamp": 1,
        "reason": 1,
    }
    return list(coll.find(date_match, projection).sort("timestamp", -1).limit(max(1, int(limit))))


def build_report(*, date_from: Optional[str], date_to: Optional[str], limit: int) -> dict[str, Any]:
    client = _mongo_client()
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    vote_coll_name = str(os.getenv("MONGO_COLL_STRATEGY_VOTES") or "strategy_votes").strip() or "strategy_votes"
    signal_coll_name = str(os.getenv("MONGO_COLL_TRADE_SIGNALS") or "trade_signals").strip() or "trade_signals"
    position_coll_name = str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS") or "strategy_positions").strip() or "strategy_positions"

    db = client[db_name]
    votes = db[vote_coll_name]
    signals = db[signal_coll_name]
    positions = db[position_coll_name]
    date_match = _date_filter(date_from=date_from, date_to=date_to)

    vote_by_strategy_pipeline = [
        {"$match": date_match},
        {"$group": {"_id": {"strategy": "$strategy", "regime": "$regime"}, "votes": {"$sum": 1}, "avg_confidence": {"$avg": "$confidence"}}},
        {"$sort": {"_id.strategy": 1, "_id.regime": 1}},
    ]
    signal_by_strategy_pipeline = [
        {"$match": date_match},
        {"$group": {"_id": {"signal_type": "$signal_type", "regime": "$regime"}, "signals": {"$sum": 1}, "avg_confidence": {"$avg": "$confidence"}}},
        {"$sort": {"_id.signal_type": 1, "_id.regime": 1}},
    ]
    position_close_pipeline = [
        {"$match": {"event": "POSITION_CLOSE", **date_match}},
        {"$group": {"_id": "$exit_reason", "count": {"$sum": 1}, "avg_pnl_pct": {"$avg": "$payload.position.pnl_pct"}}},
        {"$sort": {"count": -1}},
    ]

    report = {
        "generated_at": isoformat_ist(),
        "db": db_name,
        "collections": {
            "strategy_votes": vote_coll_name,
            "trade_signals": signal_coll_name,
            "strategy_positions": position_coll_name,
        },
        "filters": {
            "date_from": date_from,
            "date_to": date_to,
        },
        "counts": {
            "votes": votes.count_documents(date_match),
            "signals": signals.count_documents(date_match),
            "position_events": positions.count_documents(date_match),
        },
        "vote_summary_by_strategy_regime": _aggregate_to_list(votes.aggregate(vote_by_strategy_pipeline)),
        "signal_summary_by_type_regime": _aggregate_to_list(signals.aggregate(signal_by_strategy_pipeline)),
        "position_close_summary": _aggregate_to_list(positions.aggregate(position_close_pipeline)),
        "latest_signals": _latest_docs(signals, limit=limit, date_match=date_match),
        "latest_votes": _latest_docs(votes, limit=limit, date_match=date_match),
    }
    client.close()
    return report


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mongo summary report for strategy votes and signals")
    parser.add_argument("--date-from", default=None, help="Inclusive IST trade_date_ist lower bound YYYY-MM-DD")
    parser.add_argument("--date-to", default=None, help="Inclusive IST trade_date_ist upper bound YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=10, help="Latest signals/votes to include")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = build_report(date_from=args.date_from, date_to=args.date_to, limit=int(args.limit))
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
