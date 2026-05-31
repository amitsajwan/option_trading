"""Stage 6 — Execution consumer.

Reads:  risk_decisions   (Namespace.stream_for("risk_decisions"))
Writes: execution_events (Namespace.stream_for("execution_events"))
        + backward-compat pubsub topic (market:strategy:signals:v1) when
          EXECUTION_DUAL_PUBLISH=1 is set.

Assembles a TradeSignal for approved trades, publishes ExecutionEvent,
and optionally dual-publishes to the legacy trade_signal pubsub topic so
persistence_app and the dashboard continue to work unchanged.
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import datetime
from threading import Event
from typing import Optional
from uuid import uuid4

from contracts_app import build_trade_signal_event, isoformat_ist, now_ist, trade_signal_topic
from contracts_app.decision_events import build_execution_event, parse_risk_decision_event
from contracts_app.sim_namespace import Namespace

from ..contracts import Direction, SignalType, TradeSignal
from ..runtime.stage_bus import StageBus
from ._utils import is_sentinel, parse_payload_from_fields, safe_float, safe_int

logger = logging.getLogger(__name__)

_CONSUMER_GROUP = "execution-events-grp-1"
_PLUGIN_ID = "execution_consumer_v1"
_PLUGIN_VERSION = "1.0"


def _dual_publish_enabled() -> bool:
    return str(os.getenv("EXECUTION_DUAL_PUBLISH") or "0").strip().lower() in {"1", "true", "yes"}


class ExecutionConsumer:
    """Converts approved RiskDecisionEvents into ExecutionEvents (and TradeSignals).

    When ``EXECUTION_DUAL_PUBLISH=1`` this consumer also publishes to the
    legacy ``market:strategy:signals:v1`` pubsub topic so persistence_app
    and the dashboard receive signals unchanged during the Phase 2 transition.
    """

    def __init__(
        self,
        *,
        bus: StageBus,
        namespace: Namespace,
        consumer_name: Optional[str] = None,
    ) -> None:
        self._bus = bus
        self._ns = namespace
        self._consumer_name = str(consumer_name or f"execution-{socket.gethostname()}").strip()
        self._dual_publish = _dual_publish_enabled()
        self._bus.set_plugin(_PLUGIN_ID, _PLUGIN_VERSION)

    def run(
        self,
        *,
        stop_event: Optional[Event] = None,
        max_events: Optional[int] = None,
    ) -> int:
        in_stream = self._ns.stream_for("risk_decisions")
        out_stream = self._ns.stream_for("execution_events")

        self._bus.ensure_group(in_stream, _CONSUMER_GROUP)
        logger.info(
            "execution consumer started in_stream=%s out_stream=%s dual_publish=%s consumer=%s",
            in_stream, out_stream, self._dual_publish, self._consumer_name,
        )

        processed = 0
        read_pending = True

        while stop_event is None or not stop_event.is_set():
            if max_events is not None and processed >= max_events:
                break

            stream_id = "0" if read_pending else ">"
            batch = self._bus.consume(
                in_stream, _CONSUMER_GROUP, self._consumer_name,
                count=10, block_ms=2000, stream_id=stream_id,
            )

            if read_pending and not batch:
                read_pending = False
                continue

            for msg_id, fields in batch:
                if is_sentinel(fields):
                    logger.info("execution consumer received sentinel — stopping")
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    if stop_event is not None:
                        stop_event.set()
                    return processed

                payload = parse_payload_from_fields(fields)
                risk_event = parse_risk_decision_event(payload or {})
                if risk_event is None:
                    logger.warning("execution consumer: not a risk_decision msg_id=%s — skipping", msg_id)
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    continue

                try:
                    self._process(risk_event, out_stream)
                    processed += 1
                except Exception:
                    logger.exception("execution consumer: processing failed msg_id=%s", msg_id)
                    continue

                self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)

        logger.info("execution consumer stopped processed=%d", processed)
        return processed

    def _process(self, risk_event: dict, out_stream: str) -> None:
        trace_id = str(risk_event.get("trace_id") or "")
        parent_event_id = str(risk_event.get("event_id") or "")
        snapshot_id = str(risk_event.get("snapshot_id") or "")
        approved = bool(risk_event.get("approved"))
        signal_id = str(uuid4())
        now = now_ist()

        if not approved:
            # Emit a SKIP execution event to preserve the trace chain
            event = build_execution_event(
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                run_id=self._bus.run_id,
                parity_mode=self._bus.parity_mode.value,
                plugin_id=_PLUGIN_ID,
                plugin_version=_PLUGIN_VERSION,
                signal_type="SKIP",
                signal_id=signal_id,
                snapshot_id=snapshot_id,
            )
            self._bus.publish_decision(out_stream, event)
            return

        direction_str = str(risk_event.get("direction") or "")
        strike = safe_int(risk_event.get("strike"))
        entry_premium = safe_float(risk_event.get("entry_premium"))
        lots = int(risk_event.get("approved_lots") or 1)
        position_side = str(risk_event.get("position_side") or "LONG")

        # Publish to execution_events stream
        event = build_execution_event(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=self._bus.run_id,
            parity_mode=self._bus.parity_mode.value,
            plugin_id=_PLUGIN_ID,
            plugin_version=_PLUGIN_VERSION,
            signal_type="ENTER",
            signal_id=signal_id,
            direction=direction_str,
            strike=strike,
            entry_premium=entry_premium,
            position_side=position_side,
            lots=lots,
            snapshot_id=snapshot_id,
        )
        self._bus.publish_decision(out_stream, event)

        # Backward-compat dual-publish to legacy trade_signal pubsub topic
        if self._dual_publish:
            self._dual_publish_signal(
                signal_id=signal_id,
                snapshot_id=snapshot_id,
                direction_str=direction_str,
                strike=strike,
                entry_premium=entry_premium,
                lots=lots,
                now=now,
                trace_id=trace_id,
                run_id=self._bus.run_id,
            )

    def _dual_publish_signal(
        self,
        *,
        signal_id: str,
        snapshot_id: str,
        direction_str: str,
        strike: Optional[int],
        entry_premium: Optional[float],
        lots: int,
        now: datetime,
        trace_id: str,
        run_id: str,
    ) -> None:
        signal_record = {
            "signal_id": signal_id,
            "timestamp": isoformat_ist(now),
            "snapshot_id": snapshot_id,
            "signal_type": "ENTRY",
            "direction": direction_str,
            "strike": strike,
            "entry_premium": entry_premium,
            "max_lots": lots,
            "source": "pipeline_execution_consumer",
            "run_id": run_id,
        }
        try:
            self._bus.publish(
                trade_signal_topic(),
                build_trade_signal_event(
                    signal=signal_record,
                    source="strategy_app",
                    trace_id=trace_id,
                    metadata={"run_id": run_id},
                ),
            )
        except Exception:
            logger.warning("execution consumer: dual_publish to trade_signal topic failed", exc_info=True)
