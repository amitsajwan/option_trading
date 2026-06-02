"""FillTracker — consume FillEvents from Redis stream and persist to MongoDB.

Reads from:  execution:fills:v1  (Redis stream, written by consumer.py)

Writes to:
  MongoDB collection  execution_fills      — raw fill record per trade
  MongoDB collection  strategy_positions   — updates fill_entry_price / fill_exit_price
                                             on the matching position document

Env vars:
  EXECUTION_FILLS_STREAM     execution:fills:v1
  MONGO_COLL_EXECUTION_FILLS execution_fills
  MONGO_COLL_STRATEGY_POSITIONS strategy_positions
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import redis
from pymongo import MongoClient
from pymongo.collection import Collection

from contracts_app import redis_connection_kwargs

logger = logging.getLogger(__name__)

_FILLS_STREAM = os.getenv("EXECUTION_FILLS_STREAM", "execution:fills:v1")
_COLL_FILLS = os.getenv("MONGO_COLL_EXECUTION_FILLS", "execution_fills")
_COLL_POSITIONS = os.getenv("MONGO_COLL_STRATEGY_POSITIONS", "strategy_positions")
_CONSUMER_GROUP = "fill_tracker_group"
_CONSUMER_NAME = "fill_tracker_01"
_BLOCK_MS = 5000          # block on XREADGROUP for up to 5s


def _mongo_client() -> MongoClient:
    host = os.getenv("MONGO_HOST", "localhost")
    port = int(os.getenv("MONGO_PORT", "27017") or "27017")
    db_name = os.getenv("MONGO_DB", "trading_ai")
    client: MongoClient = MongoClient(host=host, port=port, serverSelectionTimeoutMS=10000)
    return client


class FillTracker:
    """Consumes fill events from Redis stream and persists to MongoDB."""

    def __init__(self) -> None:
        # for_pubsub=True → socket_timeout=None, required because XREADGROUP blocks for
        # _BLOCK_MS (5s). A 2s socket timeout would fire before the block completes,
        # causing spurious timeout errors and constant retry churn.
        self._r = redis.Redis(**redis_connection_kwargs(decode_responses=True, for_pubsub=True))
        mongo = _mongo_client()
        db = mongo[os.getenv("MONGO_DB", "trading_ai")]
        self._fills_coll: Collection = db[_COLL_FILLS]
        self._positions_coll: Collection = db[_COLL_POSITIONS]
        self._ensure_consumer_group()

    def _ensure_consumer_group(self) -> None:
        try:
            self._r.xgroup_create(_FILLS_STREAM, _CONSUMER_GROUP, id="0", mkstream=True)
            logger.info("fill tracker: consumer group created stream=%s", _FILLS_STREAM)
        except redis.exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def run(self) -> None:
        logger.info("fill tracker: listening on stream=%s", _FILLS_STREAM)
        while True:
            try:
                messages = self._r.xreadgroup(
                    groupname=_CONSUMER_GROUP,
                    consumername=_CONSUMER_NAME,
                    streams={_FILLS_STREAM: ">"},
                    count=10,
                    block=_BLOCK_MS,
                )
                if not messages:
                    continue
                for _stream, entries in messages:
                    for msg_id, fields in entries:
                        self._handle_fill(msg_id, fields)
                        self._r.xack(_FILLS_STREAM, _CONSUMER_GROUP, msg_id)
            except Exception:
                logger.exception("fill tracker error — retrying in 5s")
                time.sleep(5)

    def _handle_fill(self, msg_id: str, fields: dict) -> None:
        try:
            fill = {k: v for k, v in fields.items()}
            fill["_stream_id"] = msg_id
            fill["received_at"] = datetime.now(timezone.utc).isoformat()

            signal_type = str(fill.get("signal_type") or "").upper()
            position_id = fill.get("position_id")
            fill_price = _float(fill.get("fill_price"))
            status = str(fill.get("status") or "")

            # Insert raw fill record
            self._fills_coll.insert_one(dict(fill))
            logger.info(
                "fill recorded: signal_id=%s type=%s status=%s price=%s pos=%s",
                fill.get("signal_id"), signal_type, status, fill_price, position_id,
            )

            if status != "filled" or not position_id:
                return

            # Update the matching strategy_positions doc
            if signal_type == "ENTRY":
                self._positions_coll.update_one(
                    {"position_id": position_id},
                    {"$set": {
                        "fill_entry_price": fill_price,
                        "fill_entry_order_id": fill.get("order_id"),
                        "fill_entry_at": fill.get("filled_at"),
                        "slippage_entry_pct": _float(fill.get("slippage_pct")),
                    }},
                )
            elif signal_type == "EXIT":
                signal_premium = _float(fill.get("signal_premium"))
                fill_entry_price = self._get_entry_fill_price(position_id)
                fill_pnl_pct: Optional[float] = None
                if fill_price and fill_entry_price and fill_entry_price > 0:
                    fill_pnl_pct = (fill_price - fill_entry_price) / fill_entry_price
                self._positions_coll.update_one(
                    {"position_id": position_id},
                    {"$set": {
                        "fill_exit_price": fill_price,
                        "fill_exit_order_id": fill.get("order_id"),
                        "fill_exit_at": fill.get("filled_at"),
                        "fill_pnl_pct": fill_pnl_pct,
                        "slippage_exit_pct": _float(fill.get("slippage_pct")),
                    }},
                )
                if fill_pnl_pct is not None:
                    logger.info(
                        "fill P&L written: pos=%s fill_pnl=%.3f%%", position_id, fill_pnl_pct * 100
                    )
        except Exception:
            logger.exception("fill tracker: error handling msg_id=%s", msg_id)

    def _get_entry_fill_price(self, position_id: str) -> Optional[float]:
        doc = self._positions_coll.find_one({"position_id": position_id}, {"fill_entry_price": 1})
        if doc:
            return _float(doc.get("fill_entry_price"))
        return None


def _float(v) -> Optional[float]:
    if v is None or v == "None" or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None
