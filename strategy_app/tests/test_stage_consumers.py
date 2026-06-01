"""Tests for all 6 stream-native stage consumers (Phase 2 DoD).

All tests use MockEventBus — no live Redis required.
The pipeline is exercised end-to-end: a synthetic market snapshot event
flows through all 6 stages and produces the correct event chain.
"""
from __future__ import annotations

import json
from threading import Event
from typing import Any
from uuid import uuid4

import pytest

from contracts_app.decision_events import (
    parse_direction_decision_event,
    parse_entry_decision_event,
    parse_execution_event,
    parse_regime_decision_event,
    parse_risk_decision_event,
    parse_strike_decision_event,
)
from contracts_app.event_bus import EventBus
from contracts_app.parity_mode import ParityMode
from contracts_app.sim_namespace import Namespace, resolve_namespace
from strategy_app.consumers import (
    DirectionDecisionConsumer,
    EntryDecisionConsumer,
    ExecutionConsumer,
    RegimeDecisionConsumer,
    RiskDecisionConsumer,
    StrikeDecisionConsumer,
)
from strategy_app.runtime.stage_bus import StageBus, StageBusConfig


# ---------------------------------------------------------------------------
# MockEventBus that acts as both publisher and stream source
# ---------------------------------------------------------------------------


class MockEventBus(EventBus):
    """In-memory event bus for testing.

    Messages published are automatically available to the next consume() call
    so a single test can drive the full 6-stage pipeline without Redis.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.acknowledged: list[tuple[str, str, str]] = []
        self.groups_ensured: list[tuple[str, str]] = []
        # stream_name → queue of (msg_id, fields)
        self._queues: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def publish(self, stream: str, event: dict[str, Any]) -> None:
        self.published.append((stream, dict(event)))
        if stream.startswith("stream:"):
            # Wrap the event in the same shape as RedisEventBus produces
            fields = {
                "payload": json.dumps(event),
                "run_id": str(event.get("run_id") or ""),
            }
            self._queues.setdefault(stream, []).append((str(uuid4()), fields))

    def consume(self, stream, group, consumer, *, count=10, block_ms=2000, stream_id=">"):
        queue = self._queues.get(stream, [])
        if not queue:
            return []
        batch = queue[:count]
        del queue[:count]
        return batch

    def acknowledge(self, stream: str, group: str, message_id: str) -> None:
        self.acknowledged.append((stream, group, message_id))

    def ensure_group(self, stream: str, group: str) -> None:
        self.groups_ensured.append((stream, group))

    def ping(self) -> bool:
        return True

    def inject(self, stream: str, event: dict[str, Any]) -> None:
        """Inject a pre-built event into a stream for consumption."""
        fields = {"payload": json.dumps(event), "run_id": ""}
        self._queues.setdefault(stream, []).append((str(uuid4()), fields))

    def published_to(self, stream: str) -> list[dict[str, Any]]:
        return [e for s, e in self.published if s == stream]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stage_bus(mock: MockEventBus) -> StageBus:
    return StageBus(
        mock,
        StageBusConfig(
            run_id="test-run-001",
            parity_mode=ParityMode.REPLAY_FULL,
            plugin_id="test",
            plugin_version="0.1",
        ),
    )


def _sim_namespace() -> Namespace:
    return resolve_namespace("sim", run_id="test-run-001")


def _make_snapshot(
    *,
    is_expiry_day: bool = False,
    fut_return_5m: float = 0.003,
    vol_ratio: float = 1.5,
    vix_spike_flag: bool = False,
) -> dict[str, Any]:
    return {
        "snapshot_id": "snap-001",
        "session_context": {
            "date": "2026-05-31",
            "session_phase": "normal",
            "is_expiry_day": is_expiry_day,
            "vix_spike_flag": vix_spike_flag,
            "days_to_expiry": 3,
            "is_pre_close": False,
        },
        "futures_bar": {"close": 49500.0},
        "futures_derived": {
            "fut_return_5m": fut_return_5m,
            "fut_return_15m": fut_return_5m * 1.1,
            "fut_return_30m": fut_return_5m * 1.2,
            "vol_ratio": vol_ratio,
            "realized_vol_30m": 0.008,
            "fut_oi": 1_000_000.0,
            "fut_oi_change_30m": 5_000.0,
        },
        "vix_context": {
            "vix_current": 15.0,
            "vix_intraday_chg": 0.5,
            "vix_spike_flag": vix_spike_flag,
            "vix_regime": "normal",
        },
        "chain_aggregates": {"pcr": 1.05},
        "atm_options": {"atm_ce_ltp": 120.0, "atm_pe_ltp": 118.0},
        "opening_range": {
            "ready": True, "orh": 49600.0, "orl": 49400.0,
            "orh_broken": False, "orl_broken": False,
        },
        "iv_derived": {"iv_percentile": 45.0, "iv_regime": "normal"},
        "session_levels": {},
    }


def _make_snapshot_event(snapshot: dict) -> dict[str, Any]:
    """Wrap a snapshot in the SnapshotEventEnvelope format."""
    return {
        "event_type": "market_snapshot",
        "event_version": "1.0",
        "event_id": str(uuid4()),
        "trace_id": str(uuid4()),
        "source": "test",
        "published_at": "2026-05-31T09:15:00+05:30",
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot": snapshot,
        "metadata": {"run_id": "test-run-001", "source_mode": "sim"},
    }


def _stop_after(n: int) -> Event:
    """Return a stop Event that fires after n batches (never, tests drive via max_events)."""
    return Event()


# ---------------------------------------------------------------------------
# Stage 1 — RegimeDecisionConsumer
# ---------------------------------------------------------------------------


class TestRegimeDecisionConsumer:
    def _run_one(self, snapshot_kwargs=None) -> tuple[MockEventBus, Namespace]:
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)
        snapshot = _make_snapshot(**(snapshot_kwargs or {}))
        snap_event = _make_snapshot_event(snapshot)
        mock.inject(ns.stream_for("snapshots"), snap_event)

        consumer = RegimeDecisionConsumer(bus=bus, namespace=ns)
        consumer.run(max_events=1)
        return mock, ns

    def test_publishes_regime_decision_event(self):
        mock, ns = self._run_one()
        events = mock.published_to(ns.stream_for("regime_decisions"))
        assert len(events) == 1

    def test_regime_event_has_required_fields(self):
        mock, ns = self._run_one()
        event = mock.published_to(ns.stream_for("regime_decisions"))[0]
        for field in ("event_id", "trace_id", "parent_event_id", "run_id", "parity_mode", "plugin_id", "plugin_version"):
            assert event.get(field), f"missing: {field}"

    def test_regime_event_type(self):
        mock, ns = self._run_one()
        event = mock.published_to(ns.stream_for("regime_decisions"))[0]
        assert event["event_type"] == "regime_decision"

    def test_regime_event_parses_correctly(self):
        mock, ns = self._run_one()
        raw = mock.published_to(ns.stream_for("regime_decisions"))[0]
        parsed = parse_regime_decision_event(raw)
        assert parsed is not None

    def test_snapshot_summary_forwarded(self):
        mock, ns = self._run_one()
        event = mock.published_to(ns.stream_for("regime_decisions"))[0]
        assert isinstance(event.get("snapshot_summary"), dict)
        assert event["snapshot_summary"].get("snapshot_id") == "snap-001"

    def test_vix_spike_produces_avoid(self):
        mock, ns = self._run_one({"vix_spike_flag": True})
        event = mock.published_to(ns.stream_for("regime_decisions"))[0]
        assert event["regime"] == "AVOID"

    def test_trace_id_propagated_from_snapshot(self):
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)
        snapshot = _make_snapshot()
        snap_event = _make_snapshot_event(snapshot)
        trace_id = snap_event["trace_id"]
        mock.inject(ns.stream_for("snapshots"), snap_event)

        RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        event = mock.published_to(ns.stream_for("regime_decisions"))[0]
        assert event["trace_id"] == trace_id

    def test_parent_event_id_is_snapshot_event_id(self):
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)
        snapshot = _make_snapshot()
        snap_event = _make_snapshot_event(snapshot)
        snap_event_id = snap_event["event_id"]
        mock.inject(ns.stream_for("snapshots"), snap_event)

        RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        event = mock.published_to(ns.stream_for("regime_decisions"))[0]
        assert event["parent_event_id"] == snap_event_id

    def test_message_acknowledged_after_publish(self):
        mock, ns = self._run_one()
        assert len(mock.acknowledged) == 1

    def test_empty_stream_returns_zero(self):
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)
        count = RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=0)
        assert count == 0


# ---------------------------------------------------------------------------
# Stage 2 — EntryDecisionConsumer
# ---------------------------------------------------------------------------


class TestEntryDecisionConsumer:
    def _run_one(self, regime="TRENDING", confidence=0.80) -> tuple[MockEventBus, Namespace]:
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)

        regime_event = {
            "event_type": "regime_decision",
            "event_id": str(uuid4()),
            "trace_id": str(uuid4()),
            "parent_event_id": str(uuid4()),
            "run_id": "test-run-001",
            "timestamp": "2026-05-31T09:15:00+05:30",
            "parity_mode": "replay_full",
            "plugin_id": "test",
            "plugin_version": "0.1",
            "regime": regime,
            "confidence": confidence,
            "evidence": {"reason": "TRENDING_BULL"},
            "snapshot_id": "snap-001",
            "snapshot_summary": _make_snapshot(),
        }
        mock.inject(ns.stream_for("regime_decisions"), regime_event)
        EntryDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        return mock, ns

    def test_publishes_entry_decision_event(self):
        mock, ns = self._run_one()
        events = mock.published_to(ns.stream_for("entry_decisions"))
        assert len(events) == 1

    def test_entry_event_parses(self):
        mock, ns = self._run_one()
        parsed = parse_entry_decision_event(mock.published_to(ns.stream_for("entry_decisions"))[0])
        assert parsed is not None

    def test_entry_event_has_allowed_field(self):
        mock, ns = self._run_one("TRENDING", 0.90)
        event = mock.published_to(ns.stream_for("entry_decisions"))[0]
        assert "allowed" in event
        assert isinstance(event["allowed"], bool)

    def test_avoid_regime_blocked(self):
        mock, ns = self._run_one("AVOID", 0.99)
        event = mock.published_to(ns.stream_for("entry_decisions"))[0]
        assert event["allowed"] is False
        assert any("AVOID" in rc for rc in event.get("reason_codes", []))

    def test_expiry_regime_blocked(self):
        mock, ns = self._run_one("EXPIRY", 0.90)
        event = mock.published_to(ns.stream_for("entry_decisions"))[0]
        assert event["allowed"] is False

    def test_trace_id_propagated(self):
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)
        trace_id = str(uuid4())
        regime_event = {
            "event_type": "regime_decision", "event_id": str(uuid4()),
            "trace_id": trace_id, "parent_event_id": str(uuid4()),
            "run_id": "r1", "timestamp": "2026-05-31T09:15:00+05:30",
            "parity_mode": "replay_full", "plugin_id": "t", "plugin_version": "1",
            "regime": "TRENDING", "confidence": 0.8, "evidence": {},
            "snapshot_id": "s1", "snapshot_summary": _make_snapshot(),
        }
        mock.inject(ns.stream_for("regime_decisions"), regime_event)
        EntryDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        event = mock.published_to(ns.stream_for("entry_decisions"))[0]
        assert event["trace_id"] == trace_id


# ---------------------------------------------------------------------------
# Stage 3 — DirectionDecisionConsumer
# ---------------------------------------------------------------------------


class TestDirectionDecisionConsumer:
    def _make_entry_event(self, allowed=True, direction="CE") -> dict:
        return {
            "event_type": "entry_decision", "event_id": str(uuid4()),
            "trace_id": str(uuid4()), "parent_event_id": str(uuid4()),
            "run_id": "r1", "timestamp": "2026-05-31T09:15:00+05:30",
            "parity_mode": "replay_full", "plugin_id": "t", "plugin_version": "1",
            "allowed": allowed, "confidence": 0.80, "reason_codes": [],
            "regime": "TRENDING", "snapshot_id": "snap-001",
            "snapshot_summary": _make_snapshot(fut_return_5m=0.003 if direction == "CE" else -0.003),
            "strategy_votes": [{
                "strategy_name": "PIPELINE_ENTRY", "direction": direction,
                "confidence": 0.80, "proposed_entry_premium": 120.0, "signal_type": "ENTRY",
            }],
        }

    def _run_one(self, allowed=True, direction="CE") -> tuple[MockEventBus, Namespace]:
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)
        mock.inject(ns.stream_for("entry_decisions"), self._make_entry_event(allowed, direction))
        DirectionDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        return mock, ns

    def test_publishes_direction_event(self):
        mock, ns = self._run_one()
        assert len(mock.published_to(ns.stream_for("direction_decisions"))) == 1

    def test_direction_event_parses(self):
        mock, ns = self._run_one()
        parsed = parse_direction_decision_event(mock.published_to(ns.stream_for("direction_decisions"))[0])
        assert parsed is not None

    def test_blocked_entry_produces_vetoed_direction(self):
        mock, ns = self._run_one(allowed=False)
        event = mock.published_to(ns.stream_for("direction_decisions"))[0]
        assert event["vetoed"] is True
        assert event["direction"] == ""

    def test_allowed_entry_has_direction(self):
        mock, ns = self._run_one(allowed=True, direction="CE")
        event = mock.published_to(ns.stream_for("direction_decisions"))[0]
        # May be vetoed if consensus threshold not met, but direction field exists
        assert "direction" in event
        assert "vetoed" in event


# ---------------------------------------------------------------------------
# Stage 4 — StrikeDecisionConsumer
# ---------------------------------------------------------------------------


class TestStrikeDecisionConsumer:
    def _make_direction_event(self, vetoed=False, direction="CE") -> dict:
        return {
            "event_type": "direction_decision", "event_id": str(uuid4()),
            "trace_id": str(uuid4()), "parent_event_id": str(uuid4()),
            "run_id": "r1", "timestamp": "2026-05-31T09:15:00+05:30",
            "parity_mode": "replay_full", "plugin_id": "t", "plugin_version": "1",
            "vetoed": vetoed, "direction": "" if vetoed else direction,
            "confidence": 0.0 if vetoed else 1.5,
            "reason": "test", "snapshot_id": "snap-001",
            "snapshot_summary": _make_snapshot(),
            "strategy_votes": [],
        }

    def _run_one(self, vetoed=False, direction="CE") -> tuple[MockEventBus, Namespace]:
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)
        mock.inject(ns.stream_for("direction_decisions"), self._make_direction_event(vetoed, direction))
        StrikeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        return mock, ns

    def test_publishes_strike_event(self):
        mock, ns = self._run_one()
        assert len(mock.published_to(ns.stream_for("strike_decisions"))) == 1

    def test_strike_event_parses(self):
        mock, ns = self._run_one()
        parsed = parse_strike_decision_event(mock.published_to(ns.stream_for("strike_decisions"))[0])
        assert parsed is not None

    def test_vetoed_direction_produces_skipped_strike(self):
        mock, ns = self._run_one(vetoed=True)
        event = mock.published_to(ns.stream_for("strike_decisions"))[0]
        assert event["skipped"] is True
        assert event["strike"] is None

    def test_non_vetoed_direction_has_strike_field(self):
        mock, ns = self._run_one(vetoed=False)
        event = mock.published_to(ns.stream_for("strike_decisions"))[0]
        assert "strike" in event
        assert "skipped" in event


# ---------------------------------------------------------------------------
# Stage 5 — RiskDecisionConsumer
# ---------------------------------------------------------------------------


class TestRiskDecisionConsumer:
    def _make_strike_event(self, skipped=False) -> dict:
        return {
            "event_type": "strike_decision", "event_id": str(uuid4()),
            "trace_id": str(uuid4()), "parent_event_id": str(uuid4()),
            "run_id": "r1", "timestamp": "2026-05-31T09:15:00+05:30",
            "parity_mode": "replay_full", "plugin_id": "t", "plugin_version": "1",
            "skipped": skipped,
            "strike": None if skipped else 49500,
            "entry_premium": None if skipped else 120.0,
            "expiry": None, "position_side": "LONG", "direction": "CE",
            "snapshot_id": "snap-001",
            "snapshot_summary": _make_snapshot(),
            "rationale": "test",
        }

    def _run_one(self, skipped=False) -> tuple[MockEventBus, Namespace]:
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)
        mock.inject(ns.stream_for("strike_decisions"), self._make_strike_event(skipped))
        RiskDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        return mock, ns

    def test_publishes_risk_event(self):
        mock, ns = self._run_one()
        assert len(mock.published_to(ns.stream_for("risk_decisions"))) == 1

    def test_risk_event_parses(self):
        mock, ns = self._run_one()
        parsed = parse_risk_decision_event(mock.published_to(ns.stream_for("risk_decisions"))[0])
        assert parsed is not None

    def test_skipped_strike_not_approved(self):
        mock, ns = self._run_one(skipped=True)
        event = mock.published_to(ns.stream_for("risk_decisions"))[0]
        assert event["approved"] is False
        assert event["rejection_reason"] == "UPSTREAM_SKIPPED"

    def test_valid_strike_approved(self):
        mock, ns = self._run_one(skipped=False)
        event = mock.published_to(ns.stream_for("risk_decisions"))[0]
        assert event["approved"] is True
        assert int(event.get("approved_lots") or 0) >= 1


# ---------------------------------------------------------------------------
# Stage 6 — ExecutionConsumer
# ---------------------------------------------------------------------------


class TestExecutionConsumer:
    def _make_risk_event(self, approved=True) -> dict:
        return {
            "event_type": "risk_decision", "event_id": str(uuid4()),
            "trace_id": str(uuid4()), "parent_event_id": str(uuid4()),
            "run_id": "r1", "timestamp": "2026-05-31T09:15:00+05:30",
            "parity_mode": "replay_full", "plugin_id": "t", "plugin_version": "1",
            "approved": approved, "approved_lots": 1 if approved else 0,
            "rejection_reason": None if approved else "HALTED:operator_halt",
            "strike": 49500 if approved else None,
            "entry_premium": 120.0 if approved else None,
            "expiry": None, "position_side": "LONG", "direction": "CE",
            "snapshot_id": "snap-001",
        }

    def _run_one(self, approved=True) -> tuple[MockEventBus, Namespace]:
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)
        mock.inject(ns.stream_for("risk_decisions"), self._make_risk_event(approved))
        ExecutionConsumer(bus=bus, namespace=ns).run(max_events=1)
        return mock, ns

    def test_publishes_execution_event(self):
        mock, ns = self._run_one()
        assert len(mock.published_to(ns.stream_for("execution_events"))) == 1

    def test_execution_event_parses(self):
        mock, ns = self._run_one()
        parsed = parse_execution_event(mock.published_to(ns.stream_for("execution_events"))[0])
        assert parsed is not None

    def test_approved_risk_produces_enter_signal(self):
        mock, ns = self._run_one(approved=True)
        event = mock.published_to(ns.stream_for("execution_events"))[0]
        assert event["signal_type"] == "ENTER"
        assert event["lots"] == 1
        assert event["direction"] == "CE"

    def test_rejected_risk_produces_skip_signal(self):
        mock, ns = self._run_one(approved=False)
        event = mock.published_to(ns.stream_for("execution_events"))[0]
        assert event["signal_type"] == "SKIP"

    def test_execution_has_signal_id(self):
        mock, ns = self._run_one(approved=True)
        event = mock.published_to(ns.stream_for("execution_events"))[0]
        assert event.get("signal_id"), "signal_id must be non-empty"


# ---------------------------------------------------------------------------
# End-to-end pipeline test
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Drive all 6 consumers in sequence on a single snapshot."""

    def test_snapshot_flows_through_all_six_stages(self):
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)

        # Stage 1: inject snapshot → regime
        snapshot = _make_snapshot(fut_return_5m=0.004, vol_ratio=1.8)
        snap_event = _make_snapshot_event(snapshot)
        mock.inject(ns.stream_for("snapshots"), snap_event)
        RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)

        # Stage 2: regime → entry
        EntryDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)

        # Stage 3: entry → direction
        DirectionDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)

        # Stage 4: direction → strike
        StrikeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)

        # Stage 5: strike → risk
        RiskDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)

        # Stage 6: risk → execution
        ExecutionConsumer(bus=bus, namespace=ns).run(max_events=1)

        # Each stage must have produced exactly one event on its output stream
        assert len(mock.published_to(ns.stream_for("regime_decisions"))) == 1
        assert len(mock.published_to(ns.stream_for("entry_decisions"))) == 1
        assert len(mock.published_to(ns.stream_for("direction_decisions"))) == 1
        assert len(mock.published_to(ns.stream_for("strike_decisions"))) == 1
        assert len(mock.published_to(ns.stream_for("risk_decisions"))) == 1
        assert len(mock.published_to(ns.stream_for("execution_events"))) == 1

    def test_trace_id_consistent_across_all_stages(self):
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)

        snapshot = _make_snapshot()
        snap_event = _make_snapshot_event(snapshot)
        original_trace_id = snap_event["trace_id"]
        mock.inject(ns.stream_for("snapshots"), snap_event)

        RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        EntryDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        DirectionDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        StrikeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        RiskDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        ExecutionConsumer(bus=bus, namespace=ns).run(max_events=1)

        for stream in ("regime_decisions", "entry_decisions", "direction_decisions",
                       "strike_decisions", "risk_decisions", "execution_events"):
            events = mock.published_to(ns.stream_for(stream))
            assert events, f"no event published to {stream}"
            assert events[0]["trace_id"] == original_trace_id, (
                f"trace_id broken at stage {stream}: "
                f"expected {original_trace_id!r}, got {events[0]['trace_id']!r}"
            )

    def test_parent_event_id_chain_is_intact(self):
        """Each stage's parent_event_id must equal the upstream event's event_id."""
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)

        snapshot = _make_snapshot()
        snap_event = _make_snapshot_event(snapshot)
        mock.inject(ns.stream_for("snapshots"), snap_event)

        RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        EntryDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        DirectionDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        StrikeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        RiskDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        ExecutionConsumer(bus=bus, namespace=ns).run(max_events=1)

        stages = [
            "regime_decisions", "entry_decisions", "direction_decisions",
            "strike_decisions", "risk_decisions", "execution_events",
        ]
        # Upstream event_ids: snapshot → regime → entry → direction → strike → risk
        upstream_ids = [snap_event["event_id"]]
        for stream in stages[:-1]:
            events = mock.published_to(ns.stream_for(stream))
            assert events
            upstream_ids.append(events[0]["event_id"])

        for i, stream in enumerate(stages):
            events = mock.published_to(ns.stream_for(stream))
            assert events
            assert events[0]["parent_event_id"] == upstream_ids[i], (
                f"parent_event_id chain broken at {stream}"
            )

    def test_execution_event_has_all_required_envelope_fields(self):
        mock = MockEventBus()
        ns = _sim_namespace()
        bus = _make_stage_bus(mock)

        snapshot = _make_snapshot()
        mock.inject(ns.stream_for("snapshots"), _make_snapshot_event(snapshot))

        RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        EntryDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        DirectionDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        StrikeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        RiskDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        ExecutionConsumer(bus=bus, namespace=ns).run(max_events=1)

        exec_events = mock.published_to(ns.stream_for("execution_events"))
        assert exec_events
        event = exec_events[0]
        for field in ("event_id", "trace_id", "parent_event_id", "run_id",
                      "parity_mode", "plugin_id", "plugin_version"):
            assert event.get(field), f"final execution event missing: {field}"
