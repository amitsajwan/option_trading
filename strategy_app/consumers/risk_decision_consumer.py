"""Stage 5 — Risk approval consumer.

Reads:  strike_decisions (Namespace.stream_for("strike_decisions"))
Writes: risk_decisions   (Namespace.stream_for("risk_decisions"))

Maintains a stateful RiskManager per session.  Risk state is checked on
every StrikeDecisionEvent; approved trades get lot sizing applied.

WARNING: RiskManager state is in-process.  If this consumer restarts
mid-session, session_trade_count and consecutive_losses reset to zero,
effectively bypassing session limits until end-of-day.  Persist state
via RuntimeArtifactStore before this consumer is production-safe.
"""
from __future__ import annotations

import logging
import socket
from datetime import date
from threading import Event
from typing import Optional

from contracts_app.decision_events import build_risk_decision_event, parse_strike_decision_event
from contracts_app.sim_namespace import Namespace

from ..constants import DEFAULT_MAX_LOTS_PER_TRADE, resolve_lot_size
from ..market.snapshot_accessor import SnapshotAccessor
from ..risk.manager import RiskManager
from ..runtime.stage_bus import StageBus
from ._utils import is_sentinel, parse_payload_from_fields, safe_float, safe_int, snapshot_trade_date

logger = logging.getLogger(__name__)

_CONSUMER_GROUP = "risk-decisions-grp-1"
_PLUGIN_ID = "risk_manager_v1"
_PLUGIN_VERSION = "1.0"


def _compute_lots(entry_premium: Optional[float], risk_manager: RiskManager) -> int:
    """Simple lot sizing: 1 lot by default; delegates to RiskManager config when available."""
    ctx = risk_manager.context
    if entry_premium and entry_premium > 0 and ctx.capital_allocated > 0:
        notional = ctx.capital_allocated * ctx.risk_per_trade_pct
        raw = int(notional / (entry_premium * resolve_lot_size()))
        return max(1, min(raw, int(ctx.max_lots_per_trade or DEFAULT_MAX_LOTS_PER_TRADE)))
    return 1


class RiskDecisionConsumer:
    """Applies risk gates and lot sizing; publishes RiskDecisionEvent."""

    def __init__(
        self,
        *,
        bus: StageBus,
        namespace: Namespace,
        risk_manager: Optional[RiskManager] = None,
        consumer_name: Optional[str] = None,
    ) -> None:
        self._bus = bus
        self._ns = namespace
        self._risk = risk_manager if risk_manager is not None else RiskManager()
        self._consumer_name = str(consumer_name or f"risk-{socket.gethostname()}").strip()
        self._current_session: Optional[date] = None
        self._bus.set_plugin(_PLUGIN_ID, _PLUGIN_VERSION)

    def _handle_session(self, snapshot: dict) -> None:
        trade_date = snapshot_trade_date(snapshot)
        if trade_date is None:
            return
        if self._current_session is None or trade_date != self._current_session:
            if self._current_session is not None:
                try:
                    self._risk.on_session_end(self._current_session)
                except Exception:
                    logger.exception("risk consumer: session_end hook failed")
            self._risk.on_session_start(trade_date)
            self._current_session = trade_date

    def run(
        self,
        *,
        stop_event: Optional[Event] = None,
        max_events: Optional[int] = None,
    ) -> int:
        in_stream = self._ns.stream_for("strike_decisions")
        out_stream = self._ns.stream_for("risk_decisions")

        self._bus.ensure_group(in_stream, _CONSUMER_GROUP)
        logger.info(
            "risk consumer started in_stream=%s out_stream=%s consumer=%s",
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
                    logger.info("risk consumer received sentinel — stopping")
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    if stop_event is not None:
                        stop_event.set()
                    return processed

                payload = parse_payload_from_fields(fields)
                strike_event = parse_strike_decision_event(payload or {})
                if strike_event is None:
                    logger.warning("risk consumer: not a strike_decision msg_id=%s — skipping", msg_id)
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    continue

                try:
                    self._process(strike_event, out_stream)
                    processed += 1
                except Exception:
                    logger.exception("risk consumer: processing failed msg_id=%s", msg_id)
                    continue

                self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)

        logger.info("risk consumer stopped processed=%d", processed)
        return processed

    def _process(self, strike_event: dict, out_stream: str) -> None:
        trace_id = str(strike_event.get("trace_id") or "")
        parent_event_id = str(strike_event.get("event_id") or "")
        snapshot_id = str(strike_event.get("snapshot_id") or "")
        snapshot = strike_event.get("snapshot_summary") if isinstance(strike_event.get("snapshot_summary"), dict) else {}

        self._handle_session(snapshot)

        if strike_event.get("skipped"):
            self._emit(
                out_stream=out_stream,
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                snapshot_id=snapshot_id,
                approved=False,
                approved_lots=0,
                rejection_reason="UPSTREAM_SKIPPED",
                strike_event=strike_event,
            )
            return

        if self._risk.is_halted:
            reason = self._risk.halt_reason or "risk_halt"
            self._emit(
                out_stream=out_stream,
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                snapshot_id=snapshot_id,
                approved=False,
                approved_lots=0,
                rejection_reason=f"HALTED:{reason}",
                strike_event=strike_event,
            )
            return

        if self._risk.is_paused:
            reason = self._risk.pause_reason or "risk_pause"
            self._emit(
                out_stream=out_stream,
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                snapshot_id=snapshot_id,
                approved=False,
                approved_lots=0,
                rejection_reason=f"PAUSED:{reason}",
                strike_event=strike_event,
            )
            return

        entry_premium = safe_float(strike_event.get("entry_premium"))
        approved_lots = _compute_lots(entry_premium, self._risk)

        self._emit(
            out_stream=out_stream,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            snapshot_id=snapshot_id,
            approved=True,
            approved_lots=approved_lots,
            rejection_reason=None,
            strike_event=strike_event,
        )

    def _emit(
        self,
        *,
        out_stream: str,
        trace_id: str,
        parent_event_id: str,
        snapshot_id: str,
        approved: bool,
        approved_lots: int,
        rejection_reason: Optional[str],
        strike_event: dict,
    ) -> None:
        event = build_risk_decision_event(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=self._bus.run_id,
            parity_mode=self._bus.parity_mode.value,
            plugin_id=_PLUGIN_ID,
            plugin_version=_PLUGIN_VERSION,
            approved=approved,
            approved_lots=approved_lots,
            rejection_reason=rejection_reason,
            strike=safe_int(strike_event.get("strike")),
            entry_premium=safe_float(strike_event.get("entry_premium")),
            expiry=str(strike_event.get("expiry")) if strike_event.get("expiry") else None,
            position_side=str(strike_event.get("position_side") or "LONG"),
            direction=str(strike_event.get("direction") or ""),
            snapshot_id=snapshot_id,
        )
        self._bus.publish_decision(out_stream, event)
