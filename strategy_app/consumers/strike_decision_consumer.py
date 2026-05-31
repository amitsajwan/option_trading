"""Stage 4 — Strike selection consumer.

Reads:  direction_decisions (Namespace.stream_for("direction_decisions"))
Writes: strike_decisions    (Namespace.stream_for("strike_decisions"))
"""
from __future__ import annotations

import logging
import socket
from threading import Event
from typing import Optional

from contracts_app.decision_events import build_strike_decision_event, parse_depth_decision_event
from contracts_app.sim_namespace import Namespace

from ..market.snapshot_accessor import SnapshotAccessor
from ..runtime.stage_bus import StageBus
from ..signals.option_selector import select_strike
from ._utils import atm_premium_for_direction, is_sentinel, parse_payload_from_fields

logger = logging.getLogger(__name__)

_CONSUMER_GROUP = "strike-decisions-grp-1"
_PLUGIN_ID = "strike_selector_v1"
_PLUGIN_VERSION = "1.0"


class _DirectionDecisionProxy:
    """Minimal proxy so select_strike() can read ce_prob / pe_prob attributes."""

    def __init__(self, confidence: float, direction: str) -> None:
        if direction == "CE":
            self.ce_prob = confidence
            self.pe_prob = 1.0 - confidence
        else:
            self.pe_prob = confidence
            self.ce_prob = 1.0 - confidence


class StrikeDecisionConsumer:
    """Selects the option strike; publishes StrikeDecisionEvent."""

    def __init__(
        self,
        *,
        bus: StageBus,
        namespace: Namespace,
        consumer_name: Optional[str] = None,
    ) -> None:
        self._bus = bus
        self._ns = namespace
        self._consumer_name = str(consumer_name or f"strike-{socket.gethostname()}").strip()
        self._bus.set_plugin(_PLUGIN_ID, _PLUGIN_VERSION)

    def run(
        self,
        *,
        stop_event: Optional[Event] = None,
        max_events: Optional[int] = None,
    ) -> int:
        in_stream = self._ns.stream_for("depth_decisions")
        out_stream = self._ns.stream_for("strike_decisions")

        self._bus.ensure_group(in_stream, _CONSUMER_GROUP)
        logger.info(
            "strike consumer started in_stream=%s out_stream=%s consumer=%s",
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
                    logger.info("strike consumer received sentinel — stopping")
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    if stop_event is not None:
                        stop_event.set()
                    return processed

                payload = parse_payload_from_fields(fields)
                depth_event = parse_depth_decision_event(payload or {})
                if depth_event is None:
                    logger.warning("strike consumer: not a depth_decision msg_id=%s — skipping", msg_id)
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    continue

                try:
                    self._process(depth_event, out_stream)
                    processed += 1
                except Exception:
                    logger.exception("strike consumer: processing failed msg_id=%s", msg_id)
                    continue

                self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)

        logger.info("strike consumer stopped processed=%d", processed)
        return processed

    def _process(self, depth_event: dict, out_stream: str) -> None:
        trace_id = str(depth_event.get("trace_id") or "")
        parent_event_id = str(depth_event.get("event_id") or "")
        snapshot_id = str(depth_event.get("snapshot_id") or "")

        if not depth_event.get("proceed"):
            self._emit_skipped(out_stream, trace_id, parent_event_id, snapshot_id)
            return

        direction = str(depth_event.get("direction") or "").upper()
        if direction not in ("CE", "PE"):
            self._emit_skipped(out_stream, trace_id, parent_event_id, snapshot_id)
            return

        snapshot = depth_event.get("snapshot_summary") if isinstance(depth_event.get("snapshot_summary"), dict) else {}
        snap = SnapshotAccessor(snapshot)
        # Use depth-adjusted confidence for strike selection
        confidence = float(depth_event.get("confidence") or 0.5)
        decision_proxy = _DirectionDecisionProxy(confidence, direction)

        selection = select_strike(snap, direction, decision_proxy)

        # Determine position side: CE/PE both LONG for standard option buying
        position_side = "LONG"

        # Entry premium: from strike selector or fall back to ATM LTP
        entry_premium = selection.strike and atm_premium_for_direction(snapshot, direction)

        event = build_strike_decision_event(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=self._bus.run_id,
            parity_mode=self._bus.parity_mode.value,
            plugin_id=_PLUGIN_ID,
            plugin_version=_PLUGIN_VERSION,
            skipped=selection.strike is None,
            strike=selection.strike,
            entry_premium=entry_premium,
            position_side=position_side,
            direction=direction,
            snapshot_id=snapshot_id,
            rationale=f"mode={selection.mode} reason={selection.reason}",
        )
        self._bus.publish_decision(out_stream, event)

    def _emit_skipped(
        self,
        out_stream: str,
        trace_id: str,
        parent_event_id: str,
        snapshot_id: str,
    ) -> None:
        event = build_strike_decision_event(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=self._bus.run_id,
            parity_mode=self._bus.parity_mode.value,
            plugin_id=_PLUGIN_ID,
            plugin_version=_PLUGIN_VERSION,
            skipped=True,
            snapshot_id=snapshot_id,
        )
        self._bus.publish_decision(out_stream, event)
