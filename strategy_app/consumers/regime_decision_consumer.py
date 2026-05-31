"""Stage 1 — Regime classification consumer.

Reads:  market_snapshots  (Namespace.stream_for("snapshots"))
Writes: regime_decisions  (Namespace.stream_for("regime_decisions"))

One RegimeDecisionEvent is published per snapshot.  The full snapshot
payload is forwarded in ``snapshot_summary`` so downstream consumers do
not need to re-fetch from Redis.
"""
from __future__ import annotations

import logging
import socket
from threading import Event
from typing import Optional

from contracts_app import isoformat_ist
from contracts_app.decision_events import build_regime_decision_event
from contracts_app.sim_namespace import Namespace

from ..brain.plugin import RegimePlugin
from ..market.regime_plugin_adapter import RegimeClassifierAdapter
from ..runtime.stage_bus import StageBus
from ._utils import SENTINEL_TYPE, is_sentinel, parse_payload_from_fields

logger = logging.getLogger(__name__)

_CONSUMER_GROUP = "regime-decisions-grp-1"


class RegimeDecisionConsumer:
    """Reads market snapshots, classifies regime, publishes RegimeDecisionEvent.

    Usage::

        bus = StageBus.from_env(RedisEventBus(), plugin_id="regime_classifier_v1", plugin_version="1.0")
        ns  = resolve_namespace("sim", run_id="run-001")
        consumer = RegimeDecisionConsumer(bus=bus, namespace=ns)
        consumer.run()
    """

    def __init__(
        self,
        *,
        bus: StageBus,
        namespace: Namespace,
        plugin: Optional[RegimePlugin] = None,
        consumer_name: Optional[str] = None,
    ) -> None:
        self._bus = bus
        self._ns = namespace
        self._plugin: RegimePlugin = plugin if plugin is not None else RegimeClassifierAdapter()
        self._consumer_name = str(consumer_name or f"regime-{socket.gethostname()}").strip()
        # Update bus plugin identity from the actual plugin instance
        self._bus.set_plugin(self._plugin.plugin_id, self._plugin.plugin_version)

    def run(
        self,
        *,
        stop_event: Optional[Event] = None,
        max_events: Optional[int] = None,
    ) -> int:
        """Blocking consume loop.  Returns number of snapshots processed."""
        in_stream = self._ns.stream_for("snapshots")
        out_stream = self._ns.stream_for("regime_decisions")

        self._bus.ensure_group(in_stream, _CONSUMER_GROUP)
        logger.info(
            "regime consumer started in_stream=%s out_stream=%s consumer=%s",
            in_stream, out_stream, self._consumer_name,
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
                    logger.info("regime consumer received sentinel — stopping")
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    if stop_event is not None:
                        stop_event.set()
                    return processed

                event = parse_payload_from_fields(fields)
                if event is None:
                    logger.warning("regime consumer: unparseable message id=%s — skipping", msg_id)
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    continue

                try:
                    self._process(event, out_stream)
                    processed += 1
                except Exception:
                    logger.exception("regime consumer: processing failed msg_id=%s", msg_id)
                    # Do NOT ack — message stays in PEL, re-delivered on restart.
                    continue

                self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)

        logger.info("regime consumer stopped processed=%d", processed)
        return processed

    def _process(self, snapshot_event: dict, out_stream: str) -> None:
        # Snapshot events are wrapped: event["payload"]["snapshot"] is the actual snapshot.
        # Fall back to event["snapshot"] for direct-publish formats.
        _inner = snapshot_event.get("payload") if isinstance(snapshot_event.get("payload"), dict) else {}
        snapshot = (
            _inner.get("snapshot") if isinstance(_inner.get("snapshot"), dict)
            else snapshot_event.get("snapshot") if isinstance(snapshot_event.get("snapshot"), dict)
            else {}
        )
        snapshot_id = str(
            snapshot_event.get("snapshot_id")
            or _inner.get("snapshot_id")
            or snapshot.get("snapshot_id")
            or ""
        )
        # trace_id: carry forward from upstream event; fall back to event_id
        trace_id = str(snapshot_event.get("trace_id") or snapshot_event.get("event_id") or "")
        parent_event_id = str(snapshot_event.get("event_id") or "")

        result = self._plugin.classify(snapshot, context={})

        event = build_regime_decision_event(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=self._bus.run_id,
            parity_mode=self._bus.parity_mode.value,
            plugin_id=result.plugin_id,
            plugin_version=result.plugin_version,
            regime=result.regime,
            confidence=result.confidence,
            evidence=result.evidence,
            snapshot_id=snapshot_id,
            snapshot_summary=snapshot,  # full payload forwarded for downstream stages
        )
        self._bus.publish_decision(out_stream, event)
