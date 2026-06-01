"""Tests for canonical event envelope contracts (Phase 1 DoD criterion 1-3).

All tests run without a live Redis connection via MockEventBus.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from contracts_app.decision_events import (
    build_direction_decision_event,
    build_entry_decision_event,
    build_execution_event,
    build_regime_decision_event,
    build_risk_decision_event,
    build_strike_decision_event,
    parse_direction_decision_event,
    parse_entry_decision_event,
    parse_execution_event,
    parse_regime_decision_event,
    parse_risk_decision_event,
    parse_strike_decision_event,
)
from contracts_app.event_bus import EventBus
from contracts_app.parity_mode import ParityMode, infer_parity_mode
from strategy_app.runtime.stage_bus import StageBus, StageBusConfig


# ---------------------------------------------------------------------------
# MockEventBus — no Redis required
# ---------------------------------------------------------------------------


class MockEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.acknowledged: list[tuple[str, str, str]] = []
        self.groups_ensured: list[tuple[str, str]] = []

    def publish(self, stream: str, event: dict[str, Any]) -> None:
        self.published.append((stream, dict(event)))

    def consume(self, stream, group, consumer, *, count=10, block_ms=2000, stream_id=">"):
        return []

    def acknowledge(self, stream: str, group: str, message_id: str) -> None:
        self.acknowledged.append((stream, group, message_id))

    def ensure_group(self, stream: str, group: str) -> None:
        self.groups_ensured.append((stream, group))

    def ping(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

_COMMON = dict(
    trace_id="trace-001",
    parent_event_id="parent-snap-001",
    run_id="run-2026-05-31",
    parity_mode="live_full",
    plugin_id="test_plugin",
    plugin_version="1.0",
)

_REQUIRED_BASE_FIELDS = {
    "event_id", "trace_id", "parent_event_id", "run_id",
    "timestamp", "parity_mode", "plugin_id", "plugin_version",
}


def _assert_base_fields(event: dict) -> None:
    for field in _REQUIRED_BASE_FIELDS:
        assert field in event, f"missing required field: {field}"
        assert event[field] not in (None, ""), f"field {field!r} must be non-empty, got {event[field]!r}"


def _is_valid_uuid(s: str) -> bool:
    try:
        UUID(s)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Regime decision event
# ---------------------------------------------------------------------------


class TestRegimeDecisionEvent:
    def test_build_contains_all_base_fields(self):
        event = build_regime_decision_event(**_COMMON, regime="trend", confidence=0.84)
        _assert_base_fields(event)

    def test_event_type(self):
        event = build_regime_decision_event(**_COMMON, regime="trend", confidence=0.84)
        assert event["event_type"] == "regime_decision"

    def test_auto_generated_event_id_is_uuid(self):
        event = build_regime_decision_event(**_COMMON, regime="trend", confidence=0.84)
        assert _is_valid_uuid(event["event_id"])

    def test_explicit_event_id_preserved(self):
        event = build_regime_decision_event(**_COMMON, regime="trend", confidence=0.84, event_id="my-id-123")
        assert event["event_id"] == "my-id-123"

    def test_regime_and_confidence(self):
        event = build_regime_decision_event(**_COMMON, regime="chop", confidence=0.72)
        assert event["regime"] == "chop"
        assert abs(event["confidence"] - 0.72) < 1e-9

    def test_snapshot_summary_defaults_to_empty_dict(self):
        event = build_regime_decision_event(**_COMMON, regime="trend", confidence=0.5)
        assert isinstance(event["snapshot_summary"], dict)

    def test_parse_valid_event(self):
        event = build_regime_decision_event(**_COMMON, regime="trend", confidence=0.84)
        parsed = parse_regime_decision_event(event)
        assert parsed is not None
        assert parsed["event_type"] == "regime_decision"

    def test_parse_wrong_type_returns_none(self):
        event = build_regime_decision_event(**_COMMON, regime="trend", confidence=0.84)
        event["event_type"] = "entry_decision"
        assert parse_regime_decision_event(event) is None

    def test_parse_missing_event_id_returns_none(self):
        event = build_regime_decision_event(**_COMMON, regime="trend", confidence=0.84)
        del event["event_id"]
        assert parse_regime_decision_event(event) is None

    def test_parse_empty_payload_returns_none(self):
        assert parse_regime_decision_event({}) is None


# ---------------------------------------------------------------------------
# Entry decision event
# ---------------------------------------------------------------------------


class TestEntryDecisionEvent:
    def test_build_contains_all_base_fields(self):
        event = build_entry_decision_event(**_COMMON, allowed=True)
        _assert_base_fields(event)

    def test_allowed_true(self):
        event = build_entry_decision_event(**_COMMON, allowed=True, confidence=0.9)
        assert event["allowed"] is True
        assert abs(event["confidence"] - 0.9) < 1e-9

    def test_allowed_false(self):
        event = build_entry_decision_event(**_COMMON, allowed=False, reason_codes=["TIME_GATE", "RISK_CAP"])
        assert event["allowed"] is False
        assert event["reason_codes"] == ["TIME_GATE", "RISK_CAP"]

    def test_parse_round_trip(self):
        event = build_entry_decision_event(**_COMMON, allowed=True)
        assert parse_entry_decision_event(event) is not None

    def test_strategy_votes_default_empty(self):
        event = build_entry_decision_event(**_COMMON, allowed=True)
        assert event["strategy_votes"] == []


# ---------------------------------------------------------------------------
# Direction decision event
# ---------------------------------------------------------------------------


class TestDirectionDecisionEvent:
    def test_ce_direction(self):
        event = build_direction_decision_event(**_COMMON, vetoed=False, direction="CE", confidence=0.8)
        assert event["direction"] == "CE"
        assert event["vetoed"] is False

    def test_vetoed_event_has_empty_direction(self):
        event = build_direction_decision_event(**_COMMON, vetoed=True, direction="")
        assert event["vetoed"] is True
        assert event["direction"] == ""

    def test_parse_round_trip(self):
        event = build_direction_decision_event(**_COMMON, vetoed=False, direction="PE")
        assert parse_direction_decision_event(event) is not None


# ---------------------------------------------------------------------------
# Strike decision event
# ---------------------------------------------------------------------------


class TestStrikeDecisionEvent:
    def test_build_with_strike(self):
        event = build_strike_decision_event(
            **_COMMON, skipped=False, strike=49500, entry_premium=120.5,
            position_side="LONG", direction="CE",
        )
        assert event["strike"] == 49500
        assert abs(event["entry_premium"] - 120.5) < 1e-6
        assert event["skipped"] is False

    def test_skipped_has_none_strike(self):
        event = build_strike_decision_event(**_COMMON, skipped=True)
        assert event["skipped"] is True
        assert event["strike"] is None

    def test_parse_round_trip(self):
        event = build_strike_decision_event(**_COMMON, skipped=False, strike=49000)
        assert parse_strike_decision_event(event) is not None


# ---------------------------------------------------------------------------
# Risk decision event
# ---------------------------------------------------------------------------


class TestRiskDecisionEvent:
    def test_approved(self):
        event = build_risk_decision_event(**_COMMON, approved=True, approved_lots=2)
        assert event["approved"] is True
        assert event["approved_lots"] == 2
        assert event["rejection_reason"] is None

    def test_rejected(self):
        event = build_risk_decision_event(**_COMMON, approved=False, rejection_reason="DAILY_LOSS_CAP")
        assert event["approved"] is False
        assert event["rejection_reason"] == "DAILY_LOSS_CAP"

    def test_parse_round_trip(self):
        event = build_risk_decision_event(**_COMMON, approved=True, approved_lots=1)
        assert parse_risk_decision_event(event) is not None


# ---------------------------------------------------------------------------
# Execution event
# ---------------------------------------------------------------------------


class TestExecutionEvent:
    def test_enter_signal(self):
        event = build_execution_event(
            **_COMMON, signal_type="ENTER", signal_id="sig-001",
            direction="CE", strike=49500, lots=2,
        )
        assert event["signal_type"] == "ENTER"
        assert event["lots"] == 2

    def test_skip_signal(self):
        event = build_execution_event(**_COMMON, signal_type="SKIP")
        assert event["signal_type"] == "SKIP"
        assert event["lots"] == 0

    def test_parse_round_trip(self):
        event = build_execution_event(**_COMMON, signal_type="ENTER", lots=1)
        assert parse_execution_event(event) is not None


# ---------------------------------------------------------------------------
# ParityMode
# ---------------------------------------------------------------------------


class TestParityMode:
    @pytest.mark.parametrize("source_mode,expected", [
        ("live", ParityMode.LIVE_FULL),
        ("oos", ParityMode.REPLAY_SNAPSHOT_ONLY),
        ("sim", ParityMode.REPLAY_FULL),
        ("replay", ParityMode.REPLAY_FULL),
        ("live_full", ParityMode.LIVE_FULL),
        ("replay_full", ParityMode.REPLAY_FULL),
        ("replay_snapshot_only", ParityMode.REPLAY_SNAPSHOT_ONLY),
        ("unknown_garbage", ParityMode.LIVE_FULL),  # safe fallback
        ("", ParityMode.LIVE_FULL),
    ])
    def test_infer_parity_mode(self, source_mode, expected):
        assert infer_parity_mode(source_mode) == expected

    def test_parity_mode_values_are_strings(self):
        for member in ParityMode:
            assert isinstance(member.value, str)

    def test_parity_mode_used_in_event(self):
        event = build_regime_decision_event(**_COMMON, regime="trend", confidence=0.5)
        assert event["parity_mode"] == "live_full"


# ---------------------------------------------------------------------------
# StageBus context stamping
# ---------------------------------------------------------------------------


class TestStageBus:
    def _make_bus(self, **kwargs) -> StageBus:
        mock = MockEventBus()
        config = StageBusConfig(
            run_id=kwargs.get("run_id", "run-001"),
            parity_mode=kwargs.get("parity_mode", ParityMode.LIVE_FULL),
            plugin_id=kwargs.get("plugin_id", "test_plugin"),
            plugin_version=kwargs.get("plugin_version", "1.0"),
        )
        return StageBus(mock, config)

    def test_publish_decision_stamps_run_id(self):
        bus = self._make_bus(run_id="run-xyz")
        mock = bus._bus
        bus.publish_decision("stream:test:sim:r1", {"event_type": "regime_decision"})
        _, event = mock.published[0]
        assert event["run_id"] == "run-xyz"

    def test_publish_decision_stamps_parity_mode(self):
        bus = self._make_bus(parity_mode=ParityMode.REPLAY_FULL)
        mock = bus._bus
        bus.publish_decision("stream:test:sim:r1", {"event_type": "test"})
        _, event = mock.published[0]
        assert event["parity_mode"] == "replay_full"

    def test_publish_decision_stamps_plugin_id_and_version(self):
        bus = self._make_bus(plugin_id="my_plugin", plugin_version="2.3")
        mock = bus._bus
        bus.publish_decision("stream:test:sim:r1", {"event_type": "test"})
        _, event = mock.published[0]
        assert event["plugin_id"] == "my_plugin"
        assert event["plugin_version"] == "2.3"

    def test_publish_decision_does_not_overwrite_existing_plugin_id(self):
        bus = self._make_bus(plugin_id="bus_default")
        mock = bus._bus
        bus.publish_decision("stream:test:sim:r1", {"plugin_id": "event_level_plugin"})
        _, event = mock.published[0]
        assert event["plugin_id"] == "event_level_plugin"

    def test_set_plugin_updates_context(self):
        bus = self._make_bus()
        bus.set_plugin("new_plugin", "3.0")
        assert bus.plugin_id == "new_plugin"
        assert bus.plugin_version == "3.0"

    def test_acknowledge_delegates_to_bus(self):
        bus = self._make_bus()
        mock = bus._bus
        bus.acknowledge("stream:decisions", "grp-1", "1234-0")
        assert mock.acknowledged == [("stream:decisions", "grp-1", "1234-0")]

    def test_ensure_group_delegates_to_bus(self):
        bus = self._make_bus()
        mock = bus._bus
        bus.ensure_group("stream:regime_decisions:sim:r1", "regime-grp-1")
        assert mock.groups_ensured == [("stream:regime_decisions:sim:r1", "regime-grp-1")]

    def test_ping_delegates_to_bus(self):
        bus = self._make_bus()
        assert bus.ping() is True
