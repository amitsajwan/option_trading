"""Stage 2 — Entry gate consumer.

Reads:  regime_decisions  (Namespace.stream_for("regime_decisions"))
Writes: entry_decisions   (Namespace.stream_for("entry_decisions"))

Applies the LongOptionEntryPolicy to decide whether entry is allowed for
this snapshot.  Builds a synthetic StrategyVote from the regime signal and
snapshot momentum so the policy has enough context to evaluate.
"""
from __future__ import annotations

import logging
import socket
from datetime import datetime
from threading import Event
from typing import Optional

from contracts_app import isoformat_ist, now_ist
from contracts_app.decision_events import build_entry_decision_event, parse_regime_decision_event
from contracts_app.sim_namespace import Namespace

from ..contracts import Direction, RiskContext, SignalType, StrategyVote
from ..market.regime import Regime, RegimeSignal
from ..market.snapshot_accessor import SnapshotAccessor
from ..policy.entry_policy import LongOptionEntryPolicy, PolicyConfig
from ..runtime.stage_bus import StageBus
from ._utils import atm_premium_for_direction, is_sentinel, parse_payload_from_fields, safe_float

logger = logging.getLogger(__name__)

_CONSUMER_GROUP = "entry-decisions-grp-1"
_PLUGIN_ID = "long_option_entry_policy_v1"
_PLUGIN_VERSION = "1.0"

# Regimes that skip entry evaluation entirely (no trade possible)
_SKIP_REGIMES = frozenset({"AVOID", "EXPIRY", "HIGH_VOL"})


def _regime_signal_from_event(event: dict) -> RegimeSignal:
    """Reconstruct a RegimeSignal from a RegimeDecisionEvent dict."""
    regime_str = str(event.get("regime") or "SIDEWAYS").upper()
    try:
        regime = Regime(regime_str)
    except ValueError:
        regime = Regime.SIDEWAYS
    return RegimeSignal(
        regime=regime,
        confidence=float(event.get("confidence") or 0.0),
        reason=str((event.get("evidence") or {}).get("reason") or ""),
        evidence=dict(event.get("evidence") or {}),
    )


def _infer_direction(snapshot: dict) -> Direction:
    """Infer CE/PE direction from 5-minute futures return."""
    fd = snapshot.get("futures_derived") if isinstance(snapshot.get("futures_derived"), dict) else {}
    r5m = safe_float(fd.get("fut_return_5m"))
    if r5m is None or r5m == 0.0:
        return Direction.CE  # default
    return Direction.CE if r5m > 0 else Direction.PE


def _build_synthetic_vote(snapshot: dict, regime_signal: RegimeSignal, direction: Direction) -> StrategyVote:
    """Build a minimal StrategyVote sufficient for LongOptionEntryPolicy."""
    sc = snapshot.get("session_context") if isinstance(snapshot.get("session_context"), dict) else {}
    snapshot_id = str(snapshot.get("snapshot_id") or "")
    trade_date = str(sc.get("date") or "")
    premium = atm_premium_for_direction(snapshot, direction.value)
    return StrategyVote(
        strategy_name="PIPELINE_ENTRY",
        snapshot_id=snapshot_id,
        timestamp=now_ist(),
        trade_date=trade_date,
        signal_type=SignalType.ENTRY,
        direction=direction,
        confidence=regime_signal.confidence,
        reason=f"pipeline_regime={regime_signal.regime.value}",
        proposed_entry_premium=premium,
    )


class EntryDecisionConsumer:
    """Evaluates entry gates; publishes EntryDecisionEvent.

    Maintains a per-session :class:`RiskContext` so risk limits are available
    for entry gate evaluation.  Risk state resets on ``on_session_start()``.
    """

    def __init__(
        self,
        *,
        bus: StageBus,
        namespace: Namespace,
        policy: Optional[LongOptionEntryPolicy] = None,
        consumer_name: Optional[str] = None,
    ) -> None:
        self._bus = bus
        self._ns = namespace
        self._policy = policy if policy is not None else LongOptionEntryPolicy()
        self._consumer_name = str(consumer_name or f"entry-{socket.gethostname()}").strip()
        self._risk_ctx = RiskContext()
        self._bus.set_plugin(_PLUGIN_ID, _PLUGIN_VERSION)

    def on_session_start(self) -> None:
        self._risk_ctx = RiskContext()

    def run(
        self,
        *,
        stop_event: Optional[Event] = None,
        max_events: Optional[int] = None,
    ) -> int:
        in_stream = self._ns.stream_for("regime_decisions")
        out_stream = self._ns.stream_for("entry_decisions")

        self._bus.ensure_group(in_stream, _CONSUMER_GROUP)
        logger.info(
            "entry consumer started in_stream=%s out_stream=%s consumer=%s",
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
                    logger.info("entry consumer received sentinel — stopping")
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    if stop_event is not None:
                        stop_event.set()
                    return processed

                payload = parse_payload_from_fields(fields)
                regime_event = parse_regime_decision_event(payload or {})
                if regime_event is None:
                    logger.warning("entry consumer: not a regime_decision msg_id=%s — skipping", msg_id)
                    self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)
                    continue

                try:
                    self._process(regime_event, out_stream)
                    processed += 1
                except Exception:
                    logger.exception("entry consumer: processing failed msg_id=%s", msg_id)
                    continue

                self._bus.acknowledge(in_stream, _CONSUMER_GROUP, msg_id)

        logger.info("entry consumer stopped processed=%d", processed)
        return processed

    def _process(self, regime_event: dict, out_stream: str) -> None:
        snapshot = regime_event.get("snapshot_summary") if isinstance(regime_event.get("snapshot_summary"), dict) else {}
        snapshot_id = str(regime_event.get("snapshot_id") or "")
        trace_id = str(regime_event.get("trace_id") or "")
        parent_event_id = str(regime_event.get("event_id") or "")
        regime_str = str(regime_event.get("regime") or "SIDEWAYS")

        # Fast-reject regimes that bypass entry evaluation
        if regime_str.upper() in _SKIP_REGIMES:
            self._emit(
                out_stream=out_stream,
                trace_id=trace_id,
                parent_event_id=parent_event_id,
                snapshot_id=snapshot_id,
                allowed=False,
                reason_codes=[f"BLOCKED_REGIME:{regime_str}"],
                confidence=0.0,
                regime=regime_str,
                snapshot=snapshot,
            )
            return

        regime_signal = _regime_signal_from_event(regime_event)
        direction = _infer_direction(snapshot)
        vote = _build_synthetic_vote(snapshot, regime_signal, direction)
        snap = SnapshotAccessor(snapshot)

        decision = self._policy.evaluate(snap, vote, regime_signal, self._risk_ctx)

        reason_codes = [k + ":" + v for k, v in decision.checks.items() if v.startswith("BLOCK")]

        self._emit(
            out_stream=out_stream,
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            snapshot_id=snapshot_id,
            allowed=decision.allowed,
            reason_codes=reason_codes if not decision.allowed else [],
            confidence=decision.score,
            regime=regime_str,
            snapshot=snapshot,
            strategy_votes=[{
                "strategy_name": vote.strategy_name,
                "direction": vote.direction.value if vote.direction else "",
                "confidence": vote.confidence,
                "proposed_entry_premium": vote.proposed_entry_premium,
                "signal_type": vote.signal_type.value,
            }],
        )

    def _emit(
        self,
        *,
        out_stream: str,
        trace_id: str,
        parent_event_id: str,
        snapshot_id: str,
        allowed: bool,
        reason_codes: list,
        confidence: float,
        regime: str,
        snapshot: dict,
        strategy_votes: Optional[list] = None,
    ) -> None:
        event = build_entry_decision_event(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=self._bus.run_id,
            parity_mode=self._bus.parity_mode.value,
            plugin_id=_PLUGIN_ID,
            plugin_version=_PLUGIN_VERSION,
            allowed=allowed,
            confidence=confidence,
            reason_codes=reason_codes,
            regime=regime,
            snapshot_id=snapshot_id,
            snapshot_summary=snapshot,
            strategy_votes=strategy_votes or [],
        )
        self._bus.publish_decision(out_stream, event)
