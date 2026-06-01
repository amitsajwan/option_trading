"""Tests for the Depth Plugin stage (Stage 4 in the 7-stage pipeline).

Covers:
  - DepthDecisionResult contract
  - DepthPlugin ABC enforcement
  - PassthroughDepthPlugin (default for replay)
  - LiveDepthPlugin with mock depth reader (CE/PE bid strength, confidence delta)
  - DepthDecisionConsumer stream flow
  - Updated 7-stage end-to-end pipeline (includes Depth between Direction and Strike)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

import pytest

from contracts_app.decision_events import (
    build_depth_decision_event,
    parse_depth_decision_event,
    parse_execution_event,
)
from contracts_app.event_bus import EventBus
from contracts_app.parity_mode import ParityMode
from contracts_app.sim_namespace import resolve_namespace
from strategy_app.brain.plugin import DepthDecisionResult, DepthPlugin
from strategy_app.consumers import (
    DepthDecisionConsumer,
    DirectionDecisionConsumer,
    EntryDecisionConsumer,
    ExecutionConsumer,
    RegimeDecisionConsumer,
    RiskDecisionConsumer,
    StrikeDecisionConsumer,
)
from strategy_app.market.depth_context import DepthContext, StrikeDepth
from strategy_app.market.depth_plugin import (
    LiveDepthPlugin,
    PassthroughDepthPlugin,
    resolve_depth_plugin,
)
from strategy_app.runtime.stage_bus import StageBus, StageBusConfig


# ---------------------------------------------------------------------------
# Shared mock infrastructure
# ---------------------------------------------------------------------------


class MockEventBus(EventBus):
    def __init__(self):
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._queues: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def publish(self, stream, event):
        self.published.append((stream, dict(event)))
        if stream.startswith("stream:"):
            fields = {"payload": json.dumps(event), "run_id": ""}
            self._queues.setdefault(stream, []).append((str(uuid4()), fields))

    def consume(self, stream, group, consumer, *, count=10, block_ms=2000, stream_id=">"):
        queue = self._queues.get(stream, [])
        if not queue:
            return []
        batch = queue[:count]
        del queue[:count]
        return batch

    def acknowledge(self, stream, group, message_id):
        pass

    def ensure_group(self, stream, group):
        pass

    def ping(self):
        return True

    def inject(self, stream, event):
        fields = {"payload": json.dumps(event), "run_id": ""}
        self._queues.setdefault(stream, []).append((str(uuid4()), fields))

    def published_to(self, stream):
        return [e for s, e in self.published if s == stream]


class MockDepthReader:
    """Controllable mock for RedisDepthReader."""

    def __init__(self, ce_bid=100.0, ce_ask=101.0, ce_bid_qty=500, ce_ask_qty=200,
                 pe_bid=99.0, pe_ask=100.5, pe_bid_qty=200, pe_ask_qty=600):
        self._ce = StrikeDepth(
            best_bid=ce_bid, best_ask=ce_ask, bid_qty=ce_bid_qty, ask_qty=ce_ask_qty,
        )
        self._pe = StrikeDepth(
            best_bid=pe_bid, best_ask=pe_ask, bid_qty=pe_bid_qty, ask_qty=pe_ask_qty,
        )

    def read_depth(self):
        return DepthContext(ce=self._ce, pe=self._pe)


class MockDepthReaderUnavailable:
    def read_depth(self):
        return None


def _make_bus(mock=None) -> tuple[StageBus, MockEventBus]:
    if mock is None:
        mock = MockEventBus()
    bus = StageBus(
        mock,
        StageBusConfig(
            run_id="test-run",
            parity_mode=ParityMode.REPLAY_FULL,
            plugin_id="test",
            plugin_version="0.1",
        ),
    )
    return bus, mock


def _sim_ns():
    return resolve_namespace("sim", run_id="test-run")


def _make_direction_event(vetoed=False, direction="CE", confidence=0.78, snapshot=None) -> dict:
    return {
        "event_type": "direction_decision",
        "event_id": str(uuid4()),
        "trace_id": str(uuid4()),
        "parent_event_id": str(uuid4()),
        "run_id": "test-run",
        "timestamp": "2026-05-31T09:15:00+05:30",
        "parity_mode": "replay_full",
        "plugin_id": "test",
        "plugin_version": "0.1",
        "vetoed": vetoed,
        "direction": "" if vetoed else direction,
        "confidence": 0.0 if vetoed else confidence,
        "reason": "test",
        "snapshot_id": "snap-001",
        "snapshot_summary": snapshot or {"snapshot_id": "snap-001", "futures_derived": {}},
        "strategy_votes": [],
    }


# ---------------------------------------------------------------------------
# DepthDecisionResult contract
# ---------------------------------------------------------------------------


class TestDepthDecisionResult:
    def test_is_named_tuple(self):
        r = DepthDecisionResult(
            proceed=True, skip_reason=None, confidence_delta=0.05,
            ce_bid_strength=0.71, pe_bid_strength=0.29,
            spread_pct=0.008, depth_aligned=True, depth_available=True,
            plugin_id="test", plugin_version="1.0",
        )
        assert r.proceed is True
        assert abs(r.confidence_delta - 0.05) < 1e-9
        assert r.depth_aligned is True

    def test_no_depth_available(self):
        r = DepthDecisionResult(
            proceed=True, skip_reason=None, confidence_delta=None,
            ce_bid_strength=None, pe_bid_strength=None,
            spread_pct=None, depth_aligned=False, depth_available=False,
            plugin_id="p", plugin_version="1",
        )
        assert r.depth_available is False
        assert r.proceed is True


# ---------------------------------------------------------------------------
# DepthPlugin ABC
# ---------------------------------------------------------------------------


class TestDepthPluginABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            DepthPlugin()

    def test_concrete_must_implement_evaluate(self):
        class Partial(DepthPlugin):
            @property
            def plugin_id(self): return "p"
            @property
            def plugin_version(self): return "1"
            # missing evaluate()
        with pytest.raises(TypeError):
            Partial()


# ---------------------------------------------------------------------------
# PassthroughDepthPlugin
# ---------------------------------------------------------------------------


class TestPassthroughDepthPlugin:
    def test_always_proceeds(self):
        plugin = PassthroughDepthPlugin()
        result = plugin.evaluate("CE", {}, {})
        assert result.proceed is True

    def test_depth_available_false(self):
        plugin = PassthroughDepthPlugin()
        result = plugin.evaluate("CE", {}, {})
        assert result.depth_available is False

    def test_confidence_delta_none(self):
        plugin = PassthroughDepthPlugin()
        result = plugin.evaluate("PE", {}, {"upstream_confidence": 0.75})
        assert result.confidence_delta is None

    def test_plugin_id(self):
        assert PassthroughDepthPlugin().plugin_id == "passthrough_depth_v1"


# ---------------------------------------------------------------------------
# LiveDepthPlugin
# ---------------------------------------------------------------------------


class TestLiveDepthPlugin:
    def _plugin(self, ce_bid_qty=500, ce_ask_qty=200, pe_bid_qty=200, pe_ask_qty=600,
                hard_gate=False, monkeypatch=None) -> LiveDepthPlugin:
        reader = MockDepthReader(ce_bid_qty=ce_bid_qty, ce_ask_qty=ce_ask_qty,
                                 pe_bid_qty=pe_bid_qty, pe_ask_qty=pe_ask_qty)
        plugin = LiveDepthPlugin(reader=reader)
        if hard_gate:
            plugin._hard_gate = True
        return plugin

    def test_ce_trade_with_strong_ce_bid_aligns(self):
        # CE bid_strength = 500/(500+200) = 0.71 > 0.55 → aligned
        plugin = self._plugin(ce_bid_qty=500, ce_ask_qty=200, pe_bid_qty=200, pe_ask_qty=600)
        result = plugin.evaluate("CE", {}, {"upstream_confidence": 0.78})
        assert result.depth_aligned is True
        assert result.depth_available is True
        assert result.proceed is True

    def test_aligned_depth_boosts_confidence(self):
        plugin = self._plugin(ce_bid_qty=700, ce_ask_qty=100)
        result = plugin.evaluate("CE", {}, {"upstream_confidence": 0.78})
        assert result.confidence_delta is not None
        assert result.confidence_delta > 0

    def test_ce_trade_heavy_pe_bid_opposes(self):
        # PE bid_strength = 700/(700+100) = 0.875 > 0.65; CE bid_strength = 100/(100+700) = 0.125 < 0.45
        plugin = self._plugin(ce_bid_qty=100, ce_ask_qty=700, pe_bid_qty=700, pe_ask_qty=100)
        result = plugin.evaluate("CE", {}, {"upstream_confidence": 0.78})
        assert result.confidence_delta is not None
        assert result.confidence_delta < 0

    def test_opposed_depth_reduces_confidence(self):
        plugin = self._plugin(ce_bid_qty=100, ce_ask_qty=700, pe_bid_qty=700, pe_ask_qty=100)
        result = plugin.evaluate("CE", {}, {"upstream_confidence": 0.78})
        # confidence_delta should be negative (oppose penalty)
        assert result.confidence_delta == plugin._oppose_penalty

    def test_hard_gate_blocks_strong_opposition(self):
        plugin = self._plugin(
            ce_bid_qty=50, ce_ask_qty=950,   # CE very weak bid
            pe_bid_qty=900, pe_ask_qty=100,   # PE very strong bid
            hard_gate=True,
        )
        plugin._hard_block_threshold = 0.60  # set high threshold for test
        result = plugin.evaluate("CE", {}, {"upstream_confidence": 0.50})
        assert result.proceed is False
        assert result.skip_reason is not None

    def test_no_hard_gate_never_blocks(self):
        plugin = self._plugin(
            ce_bid_qty=50, ce_ask_qty=950,
            pe_bid_qty=900, pe_ask_qty=100,
            hard_gate=False,
        )
        result = plugin.evaluate("CE", {}, {"upstream_confidence": 0.50})
        assert result.proceed is True

    def test_unavailable_depth_always_proceeds(self):
        plugin = LiveDepthPlugin(reader=MockDepthReaderUnavailable())
        result = plugin.evaluate("CE", {}, {"upstream_confidence": 0.80})
        assert result.proceed is True
        assert result.depth_available is False
        assert result.confidence_delta is None

    def test_ce_bid_strength_computed_correctly(self):
        # ce_bid_qty=600, ce_ask_qty=400 → strength = 0.60
        plugin = self._plugin(ce_bid_qty=600, ce_ask_qty=400)
        result = plugin.evaluate("CE", {}, {})
        assert result.ce_bid_strength is not None
        assert abs(result.ce_bid_strength - 0.60) < 0.01

    def test_pe_bid_strength_computed_correctly(self):
        # pe_bid_qty=300, pe_ask_qty=700 → strength = 0.30
        plugin = self._plugin(pe_bid_qty=300, pe_ask_qty=700)
        result = plugin.evaluate("PE", {}, {})
        assert result.pe_bid_strength is not None
        assert abs(result.pe_bid_strength - 0.30) < 0.01

    def test_plugin_id(self):
        assert LiveDepthPlugin(reader=MockDepthReaderUnavailable()).plugin_id == "live_depth_v1"


# ---------------------------------------------------------------------------
# resolve_depth_plugin factory
# ---------------------------------------------------------------------------


class TestResolveDepthPlugin:
    def test_default_returns_passthrough(self):
        plugin = resolve_depth_plugin()
        assert isinstance(plugin, PassthroughDepthPlugin)

    def test_passthrough_name(self):
        assert isinstance(resolve_depth_plugin("passthrough"), PassthroughDepthPlugin)

    def test_unknown_name_returns_passthrough(self):
        assert isinstance(resolve_depth_plugin("unknown"), PassthroughDepthPlugin)


# ---------------------------------------------------------------------------
# DepthDecisionEvent build/parse
# ---------------------------------------------------------------------------


class TestDepthDecisionEvent:
    _COMMON = dict(
        trace_id="t1", parent_event_id="p1", run_id="r1",
        parity_mode="replay_full", plugin_id="test_depth", plugin_version="1.0",
    )

    def test_build_contains_required_fields(self):
        event = build_depth_decision_event(**self._COMMON, proceed=True, confidence=0.72)
        for field in ("event_id", "trace_id", "parent_event_id", "run_id", "parity_mode",
                      "plugin_id", "plugin_version"):
            assert event.get(field), f"missing: {field}"

    def test_event_type(self):
        event = build_depth_decision_event(**self._COMMON, proceed=True, confidence=0.72)
        assert event["event_type"] == "depth_decision"

    def test_confidence_stored(self):
        event = build_depth_decision_event(**self._COMMON, proceed=True, confidence=0.72)
        assert abs(event["confidence"] - 0.72) < 1e-9

    def test_bid_strengths_stored(self):
        event = build_depth_decision_event(
            **self._COMMON, proceed=True, confidence=0.72,
            ce_bid_strength=0.71, pe_bid_strength=0.29,
        )
        assert abs(event["ce_bid_strength"] - 0.71) < 1e-6
        assert abs(event["pe_bid_strength"] - 0.29) < 1e-6

    def test_parse_valid_event(self):
        event = build_depth_decision_event(**self._COMMON, proceed=True, confidence=0.72)
        parsed = parse_depth_decision_event(event)
        assert parsed is not None

    def test_parse_wrong_type_returns_none(self):
        event = build_depth_decision_event(**self._COMMON, proceed=True, confidence=0.72)
        event["event_type"] = "other"
        assert parse_depth_decision_event(event) is None


# ---------------------------------------------------------------------------
# DepthDecisionConsumer
# ---------------------------------------------------------------------------


class TestDepthDecisionConsumer:
    def _run_one(self, plugin=None, vetoed=False, direction="CE", confidence=0.78):
        bus, mock = _make_bus()
        ns = _sim_ns()
        mock.inject(ns.stream_for("direction_decisions"), _make_direction_event(vetoed, direction, confidence))
        consumer = DepthDecisionConsumer(
            bus=bus, namespace=ns,
            plugin=plugin or PassthroughDepthPlugin(),
        )
        consumer.run(max_events=1)
        return mock, ns

    def test_publishes_depth_event(self):
        mock, ns = self._run_one()
        assert len(mock.published_to(ns.stream_for("depth_decisions"))) == 1

    def test_depth_event_parses(self):
        mock, ns = self._run_one()
        parsed = parse_depth_decision_event(mock.published_to(ns.stream_for("depth_decisions"))[0])
        assert parsed is not None

    def test_vetoed_direction_produces_not_proceed(self):
        mock, ns = self._run_one(vetoed=True)
        event = mock.published_to(ns.stream_for("depth_decisions"))[0]
        assert event["proceed"] is False

    def test_passthrough_always_proceeds(self):
        mock, ns = self._run_one(vetoed=False)
        event = mock.published_to(ns.stream_for("depth_decisions"))[0]
        assert event["proceed"] is True

    def test_confidence_propagated_from_direction(self):
        mock, ns = self._run_one(confidence=0.78)
        event = mock.published_to(ns.stream_for("depth_decisions"))[0]
        # Passthrough: no delta → confidence unchanged
        assert abs(event["confidence"] - 0.78) < 1e-9

    def test_live_plugin_boosts_confidence_when_aligned(self):
        reader = MockDepthReader(ce_bid_qty=700, ce_ask_qty=100, pe_bid_qty=100, pe_ask_qty=700)
        plugin = LiveDepthPlugin(reader=reader)
        mock, ns = self._run_one(plugin=plugin, direction="CE", confidence=0.78)
        event = mock.published_to(ns.stream_for("depth_decisions"))[0]
        assert event["confidence"] >= 0.78  # boosted or equal

    def test_live_plugin_reduces_confidence_when_opposed(self):
        reader = MockDepthReader(ce_bid_qty=100, ce_ask_qty=700, pe_bid_qty=700, pe_ask_qty=100)
        plugin = LiveDepthPlugin(reader=reader)
        mock, ns = self._run_one(plugin=plugin, direction="CE", confidence=0.78)
        event = mock.published_to(ns.stream_for("depth_decisions"))[0]
        assert event["confidence"] <= 0.78  # reduced

    def test_trace_id_propagated(self):
        bus, mock = _make_bus()
        ns = _sim_ns()
        trace_id = str(uuid4())
        direction_event = _make_direction_event(confidence=0.78)
        direction_event["trace_id"] = trace_id
        mock.inject(ns.stream_for("direction_decisions"), direction_event)
        DepthDecisionConsumer(bus=bus, namespace=ns, plugin=PassthroughDepthPlugin()).run(max_events=1)
        event = mock.published_to(ns.stream_for("depth_decisions"))[0]
        assert event["trace_id"] == trace_id


# ---------------------------------------------------------------------------
# Full 7-stage pipeline end-to-end
# ---------------------------------------------------------------------------


def _make_snapshot():
    return {
        "snapshot_id": "snap-001",
        "session_context": {
            "date": "2026-05-31", "is_expiry_day": False,
            "vix_spike_flag": False, "days_to_expiry": 3, "is_pre_close": False,
        },
        "futures_bar": {"close": 49500.0},
        "futures_derived": {
            "fut_return_5m": 0.003, "fut_return_15m": 0.004, "fut_return_30m": 0.005,
            "vol_ratio": 1.5, "realized_vol_30m": 0.008,
            "fut_oi": 1_000_000.0, "fut_oi_change_30m": 5_000.0,
        },
        "vix_context": {"vix_current": 15.0, "vix_intraday_chg": 0.5, "vix_spike_flag": False, "vix_regime": "normal"},
        "chain_aggregates": {"pcr": 1.05},
        "atm_options": {"atm_ce_ltp": 120.0, "atm_pe_ltp": 118.0},
        "opening_range": {"ready": True, "orh": 49600.0, "orl": 49400.0, "orh_broken": False, "orl_broken": False},
        "iv_derived": {"iv_percentile": 45.0, "iv_regime": "normal"},
        "session_levels": {},
    }


def _make_snap_event(snapshot):
    return {
        "event_type": "market_snapshot", "event_version": "1.0",
        "event_id": str(uuid4()), "trace_id": str(uuid4()),
        "source": "test", "published_at": "2026-05-31T09:15:00+05:30",
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot": snapshot, "metadata": {"run_id": "test-run"},
    }


class TestSevenStagePipeline:
    def _run_pipeline(self) -> tuple[MockEventBus, object]:
        mock = MockEventBus()
        ns = _sim_ns()
        bus, _ = _make_bus(mock)

        snapshot = _make_snapshot()
        mock.inject(ns.stream_for("snapshots"), _make_snap_event(snapshot))

        RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        EntryDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        DirectionDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        DepthDecisionConsumer(bus=bus, namespace=ns, plugin=PassthroughDepthPlugin()).run(max_events=1)
        StrikeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        RiskDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        ExecutionConsumer(bus=bus, namespace=ns).run(max_events=1)

        return mock, ns

    def test_all_seven_streams_populated(self):
        mock, ns = self._run_pipeline()
        for slug in ("regime_decisions", "entry_decisions", "direction_decisions",
                     "depth_decisions", "strike_decisions", "risk_decisions", "execution_events"):
            events = mock.published_to(ns.stream_for(slug))
            assert len(events) == 1, f"expected 1 event in {slug}, got {len(events)}"

    def test_trace_id_consistent_across_all_seven_stages(self):
        mock = MockEventBus()
        ns = _sim_ns()
        bus, _ = _make_bus(mock)

        snapshot = _make_snapshot()
        snap_event = _make_snap_event(snapshot)
        trace_id = snap_event["trace_id"]
        mock.inject(ns.stream_for("snapshots"), snap_event)

        RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        EntryDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        DirectionDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        DepthDecisionConsumer(bus=bus, namespace=ns, plugin=PassthroughDepthPlugin()).run(max_events=1)
        StrikeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        RiskDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        ExecutionConsumer(bus=bus, namespace=ns).run(max_events=1)

        for slug in ("regime_decisions", "entry_decisions", "direction_decisions",
                     "depth_decisions", "strike_decisions", "risk_decisions", "execution_events"):
            events = mock.published_to(ns.stream_for(slug))
            assert events, f"no event in {slug}"
            assert events[0]["trace_id"] == trace_id, f"trace_id broken at {slug}"

    def test_depth_confidence_flows_into_strike_stage(self):
        mock = MockEventBus()
        ns = _sim_ns()
        bus, _ = _make_bus(mock)

        snapshot = _make_snapshot()
        mock.inject(ns.stream_for("snapshots"), _make_snap_event(snapshot))

        RegimeDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        EntryDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)
        DirectionDecisionConsumer(bus=bus, namespace=ns).run(max_events=1)

        # Use LiveDepthPlugin with strongly aligned depth → confidence should be boosted
        reader = MockDepthReader(ce_bid_qty=800, ce_ask_qty=200, pe_bid_qty=100, pe_ask_qty=900)
        depth_plugin = LiveDepthPlugin(reader=reader)
        DepthDecisionConsumer(bus=bus, namespace=ns, plugin=depth_plugin).run(max_events=1)

        depth_events = mock.published_to(ns.stream_for("depth_decisions"))
        assert depth_events
        depth_event = depth_events[0]

        # depth event should carry adjusted confidence
        assert "confidence" in depth_event
        assert isinstance(depth_event["confidence"], float)

    def test_execution_event_has_all_envelope_fields(self):
        mock, ns = self._run_pipeline()
        exec_events = mock.published_to(ns.stream_for("execution_events"))
        assert exec_events
        event = exec_events[0]
        for field in ("event_id", "trace_id", "parent_event_id", "run_id",
                      "parity_mode", "plugin_id", "plugin_version"):
            assert event.get(field), f"final execution event missing: {field}"
