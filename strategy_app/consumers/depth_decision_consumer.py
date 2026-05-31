"""Stage 4 — Depth quality consumer.

Reads:  direction_decisions (Namespace.stream_for("direction_decisions"))
Writes: depth_decisions     (Namespace.stream_for("depth_decisions"))

For each DirectionDecisionEvent:
  - Vetoed direction → pass through with proceed=False (no depth evaluation)
  - Live direction   → evaluate depth via DepthPlugin
      → depth aligned → confidence boost
      → depth opposed → confidence reduction (or hard block if DEPTH_HARD_GATE=1)

The output DepthDecisionEvent carries ``confidence`` which is the upstream
direction confidence adjusted by depth.  Downstream stages (Strike, Risk)
use this adjusted confidence for sizing decisions.
"""
from __future__ import annotations

import logging
import socket
from threading import Event
from typing import Optional

from contracts_app.decision_events import build_depth_decision_event, parse_direction_decision_event
from contracts_app.sim_namespace import Namespace

from ..brain.plugin import DepthPlugin
from ..market.depth_plugin import resolve_depth_plugin
from ..runtime.stage_bus import StageBus
from ._utils import is_sentinel, parse_payload_from_fields, safe_float

logger = logging.getLogger(__name__)

_CONSUMER_GROUP = "depth-decisions-grp-1"


class DepthDecisionConsumer:
    """Evaluates order-book depth; adjusts confidence; publishes DepthDecisionEvent.

    Usage::

        bus = StageBus.from_env(RedisEventBus(), plugin_id="live_depth_v1", plugin_version="1.0")
        ns  = resolve_namespace("sim", run_id="run-001")
        consumer = DepthDecisionConsumer(bus=bus, namespace=ns)
        consumer.run()

    Set ``DEPTH_FEED_ENABLED=1`` to activate live depth reads.
    Set ``DEPTH_HARD_GATE=1`` to make depth a hard blocker (default: advisory).
    """

    def __init__(
        self,
        *,
        bus: StageBus,
        namespace: Namespace,
        plugin: Optional[DepthPlugin] = None,
        consumer_name: Optional[str] = None,
    ) -> None:
        self._bus = bus
        self._ns = namespace
        self._plugin: DepthPlugin = plugin if plugin is not None else resolve_depth_plugin()
        self._consumer_name = str(consumer_name or f"depth-{socket.gethostname()}").strip()
        self._bus.set_plugin(self._plugin.plugin_id, self._plugin.plugin_version)

    def run(
        self,
        *,
        stop_event: Optional[Event] = None,
        max_events: Optional[int] = None,
    ) -> int:
        in_stream = self._ns.stream_for("direction_decisions")
        out_stream = self._ns.stream_for("depth_decisions")

        self._bus.ensure_group(in_stream, _CONSUMER_GROUP)
        logger.info(
            "depth consumer started in_stream=%s out_stream=%s plugin=%s consumer=%s",
            in_stream, out_stream, self._plugin.plugin_id, self._consumer_name,
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
                    logger.info("depth consumer received sentinel — stopping")
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    if stop_event is not None:
                        stop_event.set()
                    return processed

                payload = parse_payload_from_fields(fields)
                direction_event = parse_direction_decision_event(payload or {})
                if direction_event is None:
                    logger.warning("depth consumer: not a direction_decision msg_id=%s — skipping", msg_id)
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    continue

                try:
                    self._process(direction_event, out_stream)
                    processed += 1
                except Exception:
                    logger.exception("depth consumer: processing failed msg_id=%s", msg_id)
                    continue

                self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)

        logger.info("depth consumer stopped processed=%d", processed)
        return processed

    def _process(self, direction_event: dict, out_stream: str) -> None:
        trace_id = str(direction_event.get("trace_id") or "")
        parent_event_id = str(direction_event.get("event_id") or "")
        snapshot_id = str(direction_event.get("snapshot_id") or "")
        direction = str(direction_event.get("direction") or "")
        upstream_confidence = float(direction_event.get("confidence") or 0.0)
        snapshot = direction_event.get("snapshot_summary") if isinstance(direction_event.get("snapshot_summary"), dict) else {}

        # Vetoed direction — pass through without touching depth
        if direction_event.get("vetoed"):
            self._emit(
                out_stream=out_stream,
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                snapshot_id=snapshot_id,
                direction=direction,
                proceed=False,
                confidence=0.0,
                skip_reason="DIRECTION_VETOED",
                result_fields={},
                snapshot=snapshot,
                strategy_votes=list(direction_event.get("strategy_votes") or []),
            )
            return

        # Evaluate depth
        context = {"upstream_confidence": upstream_confidence}
        result = self._plugin.evaluate(direction, snapshot, context)

        # Apply confidence delta to upstream confidence
        if result.confidence_delta is not None:
            adjusted = max(0.0, min(1.0, upstream_confidence + result.confidence_delta))
        else:
            adjusted = upstream_confidence

        self._emit(
            out_stream=out_stream,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            snapshot_id=snapshot_id,
            direction=direction,
            proceed=result.proceed,
            confidence=adjusted,
            skip_reason=result.skip_reason,
            result_fields={
                "ce_bid_strength": result.ce_bid_strength,
                "pe_bid_strength": result.pe_bid_strength,
                "spread_pct": result.spread_pct,
                "depth_aligned": result.depth_aligned,
                "depth_available": result.depth_available,
            },
            snapshot=snapshot,
            strategy_votes=list(direction_event.get("strategy_votes") or []),
        )

    def _emit(
        self,
        *,
        out_stream: str,
        trace_id: str,
        parent_event_id: str,
        snapshot_id: str,
        direction: str,
        proceed: bool,
        confidence: float,
        skip_reason: Optional[str],
        result_fields: dict,
        snapshot: dict,
        strategy_votes: list,
    ) -> None:
        event = build_depth_decision_event(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=self._bus.run_id,
            parity_mode=self._bus.parity_mode.value,
            plugin_id=self._plugin.plugin_id,
            plugin_version=self._plugin.plugin_version,
            proceed=proceed,
            confidence=confidence,
            skip_reason=skip_reason,
            direction=direction,
            ce_bid_strength=result_fields.get("ce_bid_strength"),
            pe_bid_strength=result_fields.get("pe_bid_strength"),
            spread_pct=result_fields.get("spread_pct"),
            depth_aligned=bool(result_fields.get("depth_aligned", False)),
            depth_available=bool(result_fields.get("depth_available", False)),
            snapshot_id=snapshot_id,
            snapshot_summary=snapshot,
            strategy_votes=strategy_votes,
        )
        self._bus.publish_decision(out_stream, event)
