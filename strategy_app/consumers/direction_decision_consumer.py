"""Stage 3 — Direction (CE / PE) consumer.

Reads:  entry_decisions     (Namespace.stream_for("entry_decisions"))
Writes: direction_decisions (Namespace.stream_for("direction_decisions"))

When entry is allowed, calls resolve_direction_consensus() using the
strategy votes carried in the EntryDecisionEvent plus snapshot momentum
as additional signals.  When entry was vetoed, passes through a vetoed
DirectionDecisionEvent to preserve the full trace chain.
"""
from __future__ import annotations

import logging
import socket
from threading import Event
from typing import Optional

from contracts_app.decision_events import build_direction_decision_event, parse_entry_decision_event
from contracts_app.sim_namespace import Namespace

from ..contracts import Direction, SignalType, StrategyVote
from ..engines.direction_consensus import (
    extract_ml_direction_hint,
    resolve_direction_consensus,
)
from ..market.snapshot_accessor import SnapshotAccessor
from ..runtime.stage_bus import StageBus
from ._utils import is_sentinel, now_iso, parse_payload_from_fields, safe_float
from .entry_decision_consumer import _infer_direction  # reuse momentum hint

logger = logging.getLogger(__name__)

_CONSUMER_GROUP = "direction-decisions-grp-1"
_PLUGIN_ID = "direction_consensus_v1"
_PLUGIN_VERSION = "1.0"


def _deserialize_votes(raw_votes: list) -> list[StrategyVote]:
    """Reconstruct StrategyVote objects from the serialized dicts in entry event."""
    from datetime import datetime
    votes = []
    for v in raw_votes or []:
        if not isinstance(v, dict):
            continue
        dir_str = str(v.get("direction") or "").upper()
        try:
            direction = Direction(dir_str) if dir_str in ("CE", "PE") else None
        except ValueError:
            direction = None
        sig_str = str(v.get("signal_type") or "ENTRY").upper()
        try:
            signal_type = SignalType(sig_str)
        except ValueError:
            signal_type = SignalType.ENTRY
        votes.append(StrategyVote(
            strategy_name=str(v.get("strategy_name") or "PIPELINE"),
            snapshot_id=str(v.get("snapshot_id") or ""),
            timestamp=datetime.now(),
            trade_date="",
            signal_type=signal_type,
            direction=direction,
            confidence=float(v.get("confidence") or 0.5),
            reason="deserialized",
            proposed_entry_premium=safe_float(v.get("proposed_entry_premium")),
        ))
    return votes


class DirectionDecisionConsumer:
    """Resolves CE vs PE direction; publishes DirectionDecisionEvent."""

    def __init__(
        self,
        *,
        bus: StageBus,
        namespace: Namespace,
        consumer_name: Optional[str] = None,
    ) -> None:
        self._bus = bus
        self._ns = namespace
        self._consumer_name = str(consumer_name or f"direction-{socket.gethostname()}").strip()
        self._bus.set_plugin(_PLUGIN_ID, _PLUGIN_VERSION)

    def run(
        self,
        *,
        stop_event: Optional[Event] = None,
        max_events: Optional[int] = None,
    ) -> int:
        in_stream = self._ns.stream_for("entry_decisions")
        out_stream = self._ns.stream_for("direction_decisions")

        self._bus.ensure_group(in_stream, _CONSUMER_GROUP)
        logger.info(
            "direction consumer started in_stream=%s out_stream=%s consumer=%s",
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
                    logger.info("direction consumer received sentinel — stopping")
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    if stop_event is not None:
                        stop_event.set()
                    return processed

                payload = parse_payload_from_fields(fields)
                entry_event = parse_entry_decision_event(payload or {})
                if entry_event is None:
                    logger.warning("direction consumer: not an entry_decision msg_id=%s — skipping", msg_id)
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    continue

                try:
                    self._process(entry_event, out_stream)
                    processed += 1
                except Exception:
                    logger.exception("direction consumer: processing failed msg_id=%s", msg_id)
                    continue

                self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)

        logger.info("direction consumer stopped processed=%d", processed)
        return processed

    def _process(self, entry_event: dict, out_stream: str) -> None:
        trace_id = str(entry_event.get("trace_id") or "")
        parent_event_id = str(entry_event.get("event_id") or "")
        snapshot_id = str(entry_event.get("snapshot_id") or "")
        snapshot = entry_event.get("snapshot_summary") if isinstance(entry_event.get("snapshot_summary"), dict) else {}

        if not entry_event.get("allowed"):
            # Entry was blocked — pass through vetoed direction event
            self._emit_vetoed(
                out_stream=out_stream,
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                snapshot_id=snapshot_id,
                reason="ENTRY_NOT_ALLOWED",
                strategy_votes=list(entry_event.get("strategy_votes") or []),
            )
            return

        rule_votes = _deserialize_votes(list(entry_event.get("strategy_votes") or []))
        snap = SnapshotAccessor(snapshot)

        # Shadow/momentum direction as primary hint
        shadow_direction = _infer_direction(snapshot)
        shadow_score = abs(safe_float(
            (snapshot.get("futures_derived") or {}).get("fut_return_5m")
        ) or 0.0) * 100.0

        # ML hint from votes if available
        ml_direction_hint: Optional[Direction] = None
        ml_ce_prob: Optional[float] = None
        for vote in rule_votes:
            hint, prob = extract_ml_direction_hint(vote)
            if hint is not None:
                ml_direction_hint = hint
                ml_ce_prob = prob
                break

        result = resolve_direction_consensus(
            snap=snap,
            rule_votes=rule_votes,
            shadow_direction=shadow_direction,
            shadow_score=shadow_score,
            ml_direction_hint=ml_direction_hint,
            ml_ce_prob=ml_ce_prob,
        )

        if result.vetoed or result.direction is None:
            self._emit_vetoed(
                out_stream=out_stream,
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                snapshot_id=snapshot_id,
                reason=result.veto_reason or "direction_vetoed",
                strategy_votes=list(entry_event.get("strategy_votes") or []),
            )
            return

        event = build_direction_decision_event(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=self._bus.run_id,
            parity_mode=self._bus.parity_mode.value,
            plugin_id=_PLUGIN_ID,
            plugin_version=_PLUGIN_VERSION,
            vetoed=False,
            direction=result.direction.value,
            confidence=result.margin,
            reason=f"ce={result.ce_score:.2f} pe={result.pe_score:.2f} margin={result.margin:.2f}",
            snapshot_id=snapshot_id,
            strategy_votes=list(entry_event.get("strategy_votes") or []),
        )
        self._bus.publish_decision(out_stream, event)

    def _emit_vetoed(
        self,
        *,
        out_stream: str,
        trace_id: str,
        parent_event_id: str,
        snapshot_id: str,
        reason: str,
        strategy_votes: list,
    ) -> None:
        event = build_direction_decision_event(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=self._bus.run_id,
            parity_mode=self._bus.parity_mode.value,
            plugin_id=_PLUGIN_ID,
            plugin_version=_PLUGIN_VERSION,
            vetoed=True,
            direction="",
            confidence=0.0,
            reason=reason,
            snapshot_id=snapshot_id,
            strategy_votes=strategy_votes,
        )
        self._bus.publish_decision(out_stream, event)
