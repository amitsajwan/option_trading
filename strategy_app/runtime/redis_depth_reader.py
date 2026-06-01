"""Redis side-channel reader for live ATM option depth.

Depth data is written by ``ingestion_app/collectors/depth_collector.py`` every
``DEPTH_POLL_INTERVAL_SEC`` seconds (default 5) during market hours.

Keys (prefixed by execution mode via ``get_redis_key``):
    depth:atm_ce:latest   — best bid/ask for ATM CE
    depth:atm_pe:latest   — best bid/ask for ATM PE

The reader returns None when:
  - keys are absent (replay / offline / depth feed not started)
  - data is stale beyond DEPTH_STALE_SEC (default 30)
  - JSON is malformed

All callers must treat the returned DepthContext as optional and fall back to
proxy signals (IV fade, VWAP reclaim) when it is absent.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from contracts_app import get_redis_key, redis_connection_kwargs

from ..market.depth_context import DepthContext, StrikeDepth

logger = logging.getLogger(__name__)

_KEY_CE = "depth:atm_ce:latest"
_KEY_PE = "depth:atm_pe:latest"
_DEFAULT_STALE_SEC = 30


def _redis_client():
    import redis as _redis
    return _redis.Redis(**redis_connection_kwargs(decode_responses=True))


def _parse_strike_depth(raw: Optional[str]) -> Optional[StrikeDepth]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        best_bid = float(data["best_bid"]) if data.get("best_bid") is not None else None
        best_ask = float(data["best_ask"]) if data.get("best_ask") is not None else None
        bid_qty = int(data["bid_qty"]) if data.get("bid_qty") is not None else None
        ask_qty = int(data["ask_qty"]) if data.get("ask_qty") is not None else None
        microprice = float(data["microprice"]) if data.get("microprice") is not None else None
        qty_imbalance = float(data["qty_imbalance"]) if data.get("qty_imbalance") is not None else None
        total_bid_qty = int(data["total_bid_qty"]) if data.get("total_bid_qty") is not None else None
        total_ask_qty = int(data["total_ask_qty"]) if data.get("total_ask_qty") is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    return StrikeDepth(
        best_bid=best_bid,
        best_ask=best_ask,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        instrument=str(data.get("instrument") or ""),
        fetched_at=str(data.get("fetched_at") or ""),
        microprice=microprice,
        qty_imbalance=qty_imbalance,
        total_bid_qty=total_bid_qty,
        total_ask_qty=total_ask_qty,
    )


def _is_stale(fetched_at_epoch: Optional[float], stale_sec: float) -> bool:
    if fetched_at_epoch is None:
        return True
    return (time.time() - fetched_at_epoch) > stale_sec


class RedisDepthReader:
    """Reads latest ATM depth from Redis. Thread-safe (reads only)."""

    def __init__(
        self,
        *,
        client=None,
        stale_sec: Optional[float] = None,
    ) -> None:
        self._client = client if client is not None else _redis_client()
        self._stale_sec = float(
            stale_sec
            if stale_sec is not None
            else float(os.getenv("DEPTH_STALE_SEC", str(_DEFAULT_STALE_SEC)))
        )

    def read_depth(self) -> Optional[DepthContext]:
        """Return ATM depth or None if absent / stale."""
        try:
            ce_raw = self._client.get(get_redis_key(_KEY_CE))
            pe_raw = self._client.get(get_redis_key(_KEY_PE))
        except Exception:
            logger.debug("redis depth read failed", exc_info=True)
            return None

        ce_depth = _parse_strike_depth(ce_raw)
        pe_depth = _parse_strike_depth(pe_raw)

        # Check staleness using the fetched_at field written by depth_collector
        ce_epoch = _epoch_from_depth_raw(ce_raw)
        pe_epoch = _epoch_from_depth_raw(pe_raw)

        if _is_stale(ce_epoch, self._stale_sec):
            ce_depth = None
        if _is_stale(pe_epoch, self._stale_sec):
            pe_depth = None

        if ce_depth is None and pe_depth is None:
            return None

        ctx = DepthContext(ce=ce_depth, pe=pe_depth)
        return ctx if ctx.is_available else None


def _epoch_from_depth_raw(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        epoch = data.get("fetched_at_epoch")
        if epoch is not None:
            return float(epoch)
    except Exception:
        pass
    return None


def build_depth_reader_from_env() -> Optional[RedisDepthReader]:
    """Return a RedisDepthReader when DEPTH_FEED_ENABLED=1, else None."""
    enabled = str(os.getenv("DEPTH_FEED_ENABLED") or "0").strip().lower()
    if enabled not in {"1", "true", "yes"}:
        return None
    logger.info("depth feed enabled — RedisDepthReader active")
    return RedisDepthReader()
