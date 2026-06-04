"""ExecutionConsumer — subscribe to trade signals and route to broker adapter.

Subscribes to TRADE_SIGNAL_TOPIC (Redis pubsub).
For each signal:
  ENTRY → adapter.place_entry() → poll fill → publish FillEvent to execution:fills:v1
  EXIT  → adapter.place_exit()  → poll fill → publish FillEvent to execution:fills:v1

The FillTracker (fill_tracker.py) consumes that stream and writes to MongoDB.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import redis

from contracts_app import (
    parse_trade_signal_event,
    redis_connection_kwargs,
    trade_signal_topic,
)
from strategy_app.contracts import SignalType

from .adapter.base import BrokerAdapter
from .order_manager import FillEvent, OrderManager

logger = logging.getLogger(__name__)

_FILLS_STREAM = os.getenv("EXECUTION_FILLS_STREAM", "execution:fills:v1")
_ORDER_FILL_TIMEOUT = float(os.getenv("ORDER_FILL_TIMEOUT_SEC", "30") or "30")
_ORDER_POLL_INTERVAL = float(os.getenv("ORDER_POLL_INTERVAL_SEC", "1") or "1")


def _require_live_tier() -> bool:
    """When set (default ON), only signals tagged tier=='live' reach the broker.

    This is the paper/live safety gate: the strategy emits BOTH paper- and live-tier
    signals to the same topic, but only the live book should ever place real orders.
    Fail-closed — any signal without an explicit tier=='live' is treated as paper and
    skipped. Set EXECUTION_REQUIRE_LIVE_TIER=0 to execute every signal (full-live).
    """
    return str(os.getenv("EXECUTION_REQUIRE_LIVE_TIER", "1") or "1").strip().lower() in {"1", "true", "yes"}


class ExecutionConsumer:
    """Subscribe to trade signals; route to broker; emit fill events."""

    def __init__(self, adapter: BrokerAdapter) -> None:
        self._adapter = adapter
        self._order_manager = OrderManager(
            adapter,
            fill_timeout_sec=_ORDER_FILL_TIMEOUT,
            poll_interval_sec=_ORDER_POLL_INTERVAL,
        )
        self._r = redis.Redis(**redis_connection_kwargs(decode_responses=True))
        self._r_pub = redis.Redis(**redis_connection_kwargs(decode_responses=True, for_pubsub=True))
        self._topic = trade_signal_topic()

    def run(self) -> None:
        logger.info("execution consumer: subscribing to topic=%s", self._topic)
        pubsub = self._r_pub.pubsub()
        pubsub.subscribe(self._topic)
        for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                self._handle_message(message["data"])
            except Exception:
                logger.exception("execution consumer: error handling message")

    def _handle_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("execution consumer: invalid JSON message")
            return

        event = parse_trade_signal_event(payload)
        if event is None:
            return
        # parse_trade_signal_event returns the full event wrapper; the signal fields
        # live under the "signal" body. Fall back to the wrapper itself for robustness.
        signal_body = event.get("signal") if isinstance(event.get("signal"), dict) else event

        signal_type = str(signal_body.get("signal_type") or "").upper()
        signal_id = str(signal_body.get("signal_id") or "")
        direction = signal_body.get("direction")
        strike = signal_body.get("strike")
        position_id = signal_body.get("position_id")
        entry_premium = signal_body.get("entry_premium")
        tier = str(signal_body.get("tier") or "").strip().lower()

        logger.info(
            "execution consumer: signal type=%s id=%s dir=%s strike=%s pos=%s tier=%s",
            signal_type, signal_id, direction, strike, position_id, tier or "?",
        )

        # ── Paper/live safety gate ────────────────────────────────────────────
        # Only tier=="live" signals may reach the real broker. Fail-closed: a
        # missing/paper tier is skipped for BOTH entries and exits — skipping a
        # paper entry avoids an unwanted real order, and skipping a paper exit
        # avoids a "sell-to-close" on an option the broker never bought (a naked
        # short). A live position's exit carries tier=="live" (propagated from the
        # position by the tracker), so it is never stranded.
        if _require_live_tier() and tier != "live":
            logger.info(
                "execution consumer: SKIP non-live signal (gate) type=%s id=%s tier=%s",
                signal_type, signal_id, tier or "(none)",
            )
            return

        fill: Optional[FillEvent] = None

        if signal_type == SignalType.ENTRY.value:
            order_result = self._adapter.place_entry(_dict_to_signal_stub(signal_body))
            fill = self._order_manager.place_and_confirm(
                order_result=order_result,
                signal_id=signal_id,
                signal_type="ENTRY",
                position_id=position_id,
                direction=direction,
                strike=strike,
                signal_premium=_float(entry_premium),
            )

        elif signal_type == SignalType.EXIT.value:
            order_result = self._adapter.place_exit(
                _dict_to_signal_stub(signal_body),
                _dict_to_position_stub(signal_body),
            )
            fill = self._order_manager.place_and_confirm(
                order_result=order_result,
                signal_id=signal_id,
                signal_type="EXIT",
                position_id=position_id,
                direction=direction,
                strike=strike,
                signal_premium=_float(entry_premium),
            )

        if fill is not None:
            # Shadow mode: the OrderResult carries a _shadow_paper_result attribute.
            # Emit real fill to the real stream, paper fill to the paper stream.
            if getattr(fill, "_is_shadow", False) if hasattr(fill, "_is_shadow") else False:
                pass  # handled via order_result attributes below
            order_result_obj = None  # stash in local scope for shadow handling
            self._emit_fill(fill)
            # Shadow: also emit the paper fill to the paper stream
            paper_result = getattr(
                (fill if hasattr(fill, "_shadow_paper_result") else None),
                "_shadow_paper_result",
                None,
            )
            if paper_result is not None:
                from .order_manager import FillEvent
                paper_fill = self._order_manager._make_fill_event(
                    order_result=paper_result,
                    signal_id=fill.signal_id,
                    signal_type=fill.signal_type,
                    position_id=fill.position_id,
                    direction=fill.direction,
                    strike=fill.strike,
                    signal_premium=fill.signal_premium,
                )
                self._emit_fill(paper_fill, stream_override="execution:fills:paper:v1")

    def _emit_fill(self, fill: FillEvent, *, stream_override: str | None = None) -> None:
        try:
            stream = stream_override or _FILLS_STREAM
            fields = {k: ("" if v is None else str(v)) for k, v in fill.to_dict().items()}
            self._r.xadd(stream, fields, maxlen=10000, approximate=True)
            logger.info(
                "fill emitted: stream=%s signal_id=%s status=%s fill_price=%s",
                stream, fill.signal_id, fill.status, fill.fill_price,
            )
        except Exception:
            logger.exception("execution consumer: failed to emit fill event")


def _float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _dict_to_signal_stub(d: dict):
    """Minimal duck-typed stub so adapter methods can read signal fields."""
    from datetime import date, datetime

    def _parse_date(v) -> Optional[date]:
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v)).date() if "T" in str(v) else date.fromisoformat(str(v))
        except Exception:
            return None

    class _Stub:
        signal_id = str(d.get("signal_id") or "")
        direction = d.get("direction")
        strike = d.get("strike")
        entry_premium = _float(d.get("entry_premium"))
        max_lots = int(d.get("max_lots") or 1)
        expiry = _parse_date(d.get("expiry"))
        signal_type = d.get("signal_type")

    return _Stub()


def _dict_to_position_stub(d: dict):
    """Minimal duck-typed stub for position context fields needed by adapters."""
    class _Stub:
        position_id = str(d.get("position_id") or "")
        lots = int(d.get("max_lots") or 1)
        current_premium = _float(d.get("entry_premium")) or 0.0
        entry_premium = _float(d.get("entry_premium")) or 0.0
        direction = d.get("direction")
        strike = d.get("strike")

    return _Stub()
