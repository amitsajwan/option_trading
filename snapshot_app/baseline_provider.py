from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

try:
    from pymongo import DESCENDING, MongoClient
except Exception:  # pragma: no cover
    DESCENDING = -1
    MongoClient = None


IST = timezone(timedelta(hours=5, minutes=30))


def _parse_sources(raw: str) -> list[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


class MongoPrevSessionBaseline:
    def __init__(self) -> None:
        self._client: Optional[Any] = None
        self._db: Optional[Any] = None
        self.collection_name = str(os.getenv("MONGO_COLL_OPTIONS") or "live_options_chain").strip()
        self.preferred_sources = _parse_sources(os.getenv("MONGO_BASELINE_SOURCES", "zerodha_api"))

    def _db_handle(self) -> Optional[Any]:
        if self._db is not None:
            return self._db
        if MongoClient is None:
            return None

        uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
        db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
        try:
            if uri:
                client = MongoClient(uri, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500, socketTimeoutMS=3000)
            else:
                host = str(os.getenv("MONGO_HOST") or "localhost")
                port = int(os.getenv("MONGO_PORT") or "27017")
                client = MongoClient(host=host, port=port, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500, socketTimeoutMS=3000)
            client.admin.command("ping")
            self._client = client
            self._db = client[db_name]
            return self._db
        except Exception:
            self._client = None
            self._db = None
            return None

    def get(self, *, instrument: str, trade_date_ist: date) -> Optional[dict[str, Any]]:
        db = self._db_handle()
        if db is None:
            return None
        coll = db[self.collection_name]
        instr = str(instrument or "").strip().upper()
        if not instr:
            return None

        query_parts = [
            {"$or": [{"instrument": instr}, {"futures_instrument": instr}]},
            {"trade_date_ist": {"$lt": trade_date_ist.isoformat()}},
            {"pcr": {"$ne": None}},
            {"max_pain": {"$ne": None}},
        ]
        if self.preferred_sources:
            query_parts.append({"source": {"$in": self.preferred_sources}})
        query = {"$and": query_parts}
        projection = {"trade_date_ist": 1, "timestamp": 1, "pcr": 1, "max_pain": 1, "source": 1}
        doc = coll.find_one(query, projection=projection, sort=[("trade_date_ist", DESCENDING), ("timestamp", DESCENDING)])

        if doc is None:
            day_start_ist = datetime.combine(trade_date_ist, time.min, tzinfo=IST)
            cutoff = day_start_ist
            cutoff_naive = day_start_ist.replace(tzinfo=None)
            fallback_parts = [
                {"$or": [{"instrument": instr}, {"futures_instrument": instr}]},
                {
                    "$or": [
                        {"timestamp": {"$lt": cutoff}},
                        {"timestamp": {"$lt": cutoff_naive}},
                        {"market_minute": {"$lt": cutoff}},
                        {"market_minute": {"$lt": cutoff_naive}},
                    ]
                },
                {"pcr": {"$ne": None}},
                {"max_pain": {"$ne": None}},
            ]
            if self.preferred_sources:
                fallback_parts.append({"source": {"$in": self.preferred_sources}})
            doc = coll.find_one(
                {"$and": fallback_parts},
                projection=projection,
                sort=[("timestamp", DESCENDING), ("market_minute", DESCENDING)],
            )
        if not isinstance(doc, dict):
            return None
        try:
            pcr = float(doc.get("pcr"))
            max_pain = int(round(float(doc.get("max_pain"))))
        except Exception:
            return None
        return {
            "trade_date": str(doc.get("trade_date_ist") or ""),
            "pcr": pcr,
            "max_pain": max_pain,
            "source": str(doc.get("source") or "") or None,
        }
