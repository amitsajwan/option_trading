"""Phase 3 tests — new regime labels, features, plugin enforcement.

Covers:
  - 4 new Regime enum values + detection logic
  - candle_overlap and opening_range_width_pct on SnapshotAccessor
  - profiles.py backfill for all new regime labels
  - ExtendedRegimePlugin + resolve_regime_plugin factory
  - StageBus.publish_decision() enforcement
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from contracts_app.event_bus import EventBus
from contracts_app.parity_mode import ParityMode
from strategy_app.brain.plugin import RegimeDecisionResult, RegimePlugin
from strategy_app.engines.profiles import (
    PROFILE_DET_PROD_V1,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1,
    PROFILE_TRADER_MASTER_V1,
    get_regime_entry_map,
    known_profile_ids,
)
from strategy_app.market.extended_regime_plugin import ExtendedRegimePlugin, resolve_regime_plugin
from strategy_app.market.regime import Regime, RegimeClassifier
from strategy_app.market.regime_plugin_adapter import RegimeClassifierAdapter
from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.runtime.stage_bus import StageBus, StageBusConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockEventBus(EventBus):
    def __init__(self):
        self.published = []

    def publish(self, stream, event):
        self.published.append((stream, dict(event)))

    def consume(self, stream, group, consumer, *, count=10, block_ms=2000, stream_id=">"):
        return []

    def acknowledge(self, stream, group, message_id):
        pass

    def ensure_group(self, stream, group):
        pass

    def ping(self):
        return True


def _snap(
    *,
    vix_spike_flag=False,
    is_expiry_day=False,
    vix_intraday_chg=0.5,
    realized_vol_30m=0.008,
    fut_return_5m=0.003,
    fut_return_15m=0.004,
    fut_return_30m=0.005,
    vol_ratio=1.5,
    orh_broken=False,
    orl_broken=False,
    orh=49600.0,
    orl=49400.0,
    futures_derived_extra: dict | None = None,
) -> dict[str, Any]:
    fd = {
        "fut_return_5m": fut_return_5m,
        "fut_return_15m": fut_return_15m,
        "fut_return_30m": fut_return_30m,
        "vol_ratio": vol_ratio,
        "realized_vol_30m": realized_vol_30m,
        "fut_oi": 1_000_000.0,
        "fut_oi_change_30m": 5_000.0,
    }
    if futures_derived_extra:
        fd.update(futures_derived_extra)
    return {
        "snapshot_id": "s1",
        "session_context": {
            "date": "2026-05-31",
            "is_expiry_day": is_expiry_day,
            "vix_spike_flag": vix_spike_flag,
            "days_to_expiry": 3,
            "is_pre_close": False,
        },
        "futures_bar": {"close": 49500.0, "high": 49600.0, "low": 49400.0},
        "futures_derived": fd,
        "vix_context": {
            "vix_current": 15.0,
            "vix_intraday_chg": vix_intraday_chg,
            "vix_spike_flag": vix_spike_flag,
            "vix_regime": "normal",
        },
        "chain_aggregates": {"pcr": 1.05},
        "atm_options": {"atm_ce_ltp": 120.0, "atm_pe_ltp": 118.0},
        "opening_range": {
            "ready": True, "orh": orh, "orl": orl,
            "orh_broken": orh_broken, "orl_broken": orl_broken,
        },
        "iv_derived": {"iv_percentile": 45.0, "iv_regime": "normal"},
        "session_levels": {},
    }


# ---------------------------------------------------------------------------
# Regime enum
# ---------------------------------------------------------------------------


class TestRegimeEnum:
    def test_new_labels_exist(self):
        assert Regime.CHOP.value == "CHOP"
        assert Regime.BREAKOUT.value == "BREAKOUT"
        assert Regime.PANIC.value == "PANIC"
        assert Regime.DEAD_MARKET.value == "DEAD_MARKET"

    def test_total_regime_count_is_ten(self):
        assert len(Regime) == 10

    def test_original_labels_unchanged(self):
        for label in ("TRENDING", "SIDEWAYS", "HIGH_VOL", "AVOID", "PRE_EXPIRY", "EXPIRY"):
            assert Regime(label).value == label


# ---------------------------------------------------------------------------
# New regime detection
# ---------------------------------------------------------------------------


class TestPanicDetection:
    def test_high_vix_chg_and_high_rvol_returns_panic(self):
        clf = RegimeClassifier()
        snap = SnapshotAccessor(_snap(vix_intraday_chg=6.0, realized_vol_30m=0.018))
        result = clf.classify(snap)
        assert result.regime == Regime.PANIC

    def test_low_vix_chg_does_not_trigger_panic(self):
        clf = RegimeClassifier()
        snap = SnapshotAccessor(_snap(vix_intraday_chg=1.0, realized_vol_30m=0.018))
        result = clf.classify(snap)
        assert result.regime != Regime.PANIC

    def test_vix_spike_flag_takes_priority_over_panic(self):
        clf = RegimeClassifier()
        snap = SnapshotAccessor(_snap(vix_spike_flag=True, vix_intraday_chg=8.0, realized_vol_30m=0.025))
        result = clf.classify(snap)
        assert result.regime == Regime.AVOID  # AVOID has higher priority than PANIC


class TestDeadMarketDetection:
    def test_very_low_vol_ratio_returns_dead_market(self):
        clf = RegimeClassifier()
        snap = SnapshotAccessor(_snap(vol_ratio=0.10, fut_return_5m=0.0, fut_return_15m=0.0, fut_return_30m=0.0))
        result = clf.classify(snap)
        assert result.regime == Regime.DEAD_MARKET

    def test_normal_vol_ratio_not_dead_market(self):
        clf = RegimeClassifier()
        snap = SnapshotAccessor(_snap(vol_ratio=1.5))
        result = clf.classify(snap)
        assert result.regime != Regime.DEAD_MARKET


class TestBreakoutDetection:
    def test_orh_broken_with_strong_vol_returns_breakout(self):
        clf = RegimeClassifier()
        snap = SnapshotAccessor(_snap(
            orh_broken=True, vol_ratio=2.0,
            fut_return_5m=0.004, fut_return_15m=0.005, fut_return_30m=0.006,
        ))
        result = clf.classify(snap)
        assert result.regime == Regime.BREAKOUT

    def test_orl_broken_with_strong_vol_returns_breakout(self):
        clf = RegimeClassifier()
        snap = SnapshotAccessor(_snap(
            orl_broken=True, vol_ratio=2.0,
            fut_return_5m=-0.004, fut_return_15m=-0.005, fut_return_30m=-0.006,
        ))
        result = clf.classify(snap)
        assert result.regime == Regime.BREAKOUT

    def test_orh_broken_without_strong_vol_does_not_produce_breakout(self):
        clf = RegimeClassifier()
        # vol_ratio below trend_vol_ratio_min (1.30 default) → strong_vol=False
        snap = SnapshotAccessor(_snap(
            orh_broken=True, vol_ratio=1.0,
            fut_return_5m=0.004, fut_return_15m=0.005, fut_return_30m=0.006,
        ))
        result = clf.classify(snap)
        assert result.regime != Regime.BREAKOUT

    def test_breakout_evidence_includes_key_fields(self):
        clf = RegimeClassifier()
        snap = SnapshotAccessor(_snap(orh_broken=True, vol_ratio=2.0))
        result = clf.classify(snap)
        if result.regime == Regime.BREAKOUT:
            assert "orh_broken" in result.evidence or "bull_score" in result.evidence


class TestChopDetection:
    def test_low_vol_mixed_returns_returns_chop(self):
        clf = RegimeClassifier()
        # Mixed returns (not all aligned), weak vol
        snap = SnapshotAccessor(_snap(
            vol_ratio=0.70, fut_return_5m=0.001, fut_return_15m=-0.001, fut_return_30m=0.0005,
            futures_derived_extra={"candle_overlap": 0.60},
        ))
        result = clf.classify(snap)
        assert result.regime == Regime.CHOP

    def test_strong_aligned_returns_not_chop(self):
        clf = RegimeClassifier()
        # With mixed returns, weak vol, and high candle overlap → CHOP
        # With strong aligned returns → TRENDING or SIDEWAYS (not CHOP)
        snap = SnapshotAccessor(_snap(
            vol_ratio=2.0, fut_return_5m=0.005, fut_return_15m=0.006, fut_return_30m=0.007,
        ))
        result = clf.classify(snap)
        assert result.regime != Regime.CHOP


# ---------------------------------------------------------------------------
# SnapshotAccessor new properties
# ---------------------------------------------------------------------------


class TestSnapshotAccessorNewProperties:
    def test_opening_range_width_pct_computed_from_orh_orl(self):
        snap = SnapshotAccessor(_snap(orh=49600.0, orl=49400.0))
        pct = snap.opening_range_width_pct
        assert pct is not None
        expected = (49600.0 - 49400.0) / 49400.0
        assert abs(pct - expected) < 1e-9

    def test_opening_range_width_pct_none_when_orl_zero(self):
        snap = SnapshotAccessor(_snap(orh=49600.0, orl=0.0))
        assert snap.opening_range_width_pct is None

    def test_opening_range_width_pct_none_when_or_missing(self):
        payload = {"snapshot_id": "x", "opening_range": {}}
        snap = SnapshotAccessor(payload)
        assert snap.opening_range_width_pct is None

    def test_candle_overlap_reads_from_futures_derived(self):
        payload = {
            "snapshot_id": "x",
            "futures_derived": {"candle_overlap": 0.45},
        }
        snap = SnapshotAccessor(payload)
        assert abs(snap.candle_overlap - 0.45) < 1e-9

    def test_candle_overlap_reads_from_top_level_payload(self):
        payload = {"snapshot_id": "x", "candle_overlap": 0.32}
        snap = SnapshotAccessor(payload)
        assert abs(snap.candle_overlap - 0.32) < 1e-9

    def test_candle_overlap_none_when_absent(self):
        snap = SnapshotAccessor({"snapshot_id": "x"})
        assert snap.candle_overlap is None


# ---------------------------------------------------------------------------
# profiles.py backfill
# ---------------------------------------------------------------------------


class TestProfilesBackfill:
    NEW_LABELS = ("CHOP", "BREAKOUT", "PANIC", "DEAD_MARKET")

    def test_all_profiles_have_new_labels(self):
        for profile_id in known_profile_ids():
            regime_map = get_regime_entry_map(profile_id)
            for label in self.NEW_LABELS:
                assert label in regime_map, (
                    f"profile {profile_id!r} missing regime key {label!r}"
                )

    def test_chop_inherits_sideways_or_is_deliberate_no_trade(self):
        # CHOP defaults to the SIDEWAYS strategy list via _NEW_REGIME_FALLBACKS, but a
        # profile may deliberately override CHOP to [] (no-trade). Long-premium ML-entry
        # profiles do exactly this: buying options into mixed, low-energy chop just bleeds
        # theta (profiles.py CHOP override; 2026-06-03 analysis — 3 consecutive PE losses
        # in CHOP, all TIME_STOP, MFE≈0). CHOP must therefore be EITHER the SIDEWAYS set
        # or empty — never some other arbitrary strategy list.
        for profile_id in known_profile_ids():
            regime_map = get_regime_entry_map(profile_id)
            chop = regime_map.get("CHOP")
            sideways = regime_map.get("SIDEWAYS")
            assert chop == sideways or chop == [], (
                f"profile {profile_id!r}: CHOP must inherit SIDEWAYS or be empty "
                f"(deliberate no-trade), got {chop!r}"
            )

    def test_breakout_uses_trending_strategies(self):
        for profile_id in known_profile_ids():
            regime_map = get_regime_entry_map(profile_id)
            assert regime_map.get("BREAKOUT") == regime_map.get("TRENDING"), (
                f"profile {profile_id!r}: BREAKOUT should inherit TRENDING strategies"
            )

    def test_panic_and_dead_market_are_empty(self):
        for profile_id in known_profile_ids():
            regime_map = get_regime_entry_map(profile_id)
            assert regime_map.get("PANIC") == [], (
                f"profile {profile_id!r}: PANIC should be empty"
            )
            assert regime_map.get("DEAD_MARKET") == [], (
                f"profile {profile_id!r}: DEAD_MARKET should be empty"
            )

    def test_original_strategies_unchanged(self):
        """Adding new labels must not alter existing strategy lists."""
        det = get_regime_entry_map(PROFILE_DET_PROD_V1)
        assert "IV_FILTER" in det["TRENDING"]
        assert "ORB" in det["TRENDING"]
        assert det["AVOID"] == []


# ---------------------------------------------------------------------------
# ExtendedRegimePlugin
# ---------------------------------------------------------------------------


class TestExtendedRegimePlugin:
    def test_plugin_id_and_version(self):
        plugin = ExtendedRegimePlugin()
        assert plugin.plugin_id == "extended_regime_v1"
        assert plugin.plugin_version == "1.0"

    def test_classify_returns_regime_decision_result(self):
        plugin = ExtendedRegimePlugin()
        result = plugin.classify(_snap(), context={})
        assert isinstance(result, RegimeDecisionResult)

    def test_can_return_breakout(self):
        plugin = ExtendedRegimePlugin()
        snap = _snap(orh_broken=True, vol_ratio=2.0)
        result = plugin.classify(snap, context={})
        assert result.regime == Regime.BREAKOUT.value

    def test_can_return_panic(self):
        plugin = ExtendedRegimePlugin()
        snap = _snap(vix_intraday_chg=7.0, realized_vol_30m=0.020)
        result = plugin.classify(snap, context={})
        assert result.regime == Regime.PANIC.value

    def test_can_return_dead_market(self):
        plugin = ExtendedRegimePlugin()
        snap = _snap(vol_ratio=0.10, fut_return_5m=0.0, fut_return_15m=0.0, fut_return_30m=0.0)
        result = plugin.classify(snap, context={})
        assert result.regime == Regime.DEAD_MARKET.value

    def test_configure_forwards_to_classifier(self):
        plugin = ExtendedRegimePlugin()
        plugin.configure({"panic_vix_chg_min": 3.0})
        snap = _snap(vix_intraday_chg=4.0, realized_vol_30m=0.015)
        result = plugin.classify(snap, context={})
        assert result.regime == Regime.PANIC.value


class TestResolveRegimePlugin:
    def test_default_returns_extended(self):
        plugin = resolve_regime_plugin()
        assert isinstance(plugin, ExtendedRegimePlugin)

    def test_extended_v1_returns_extended(self):
        plugin = resolve_regime_plugin("extended_v1")
        assert isinstance(plugin, ExtendedRegimePlugin)

    def test_legacy_returns_adapter(self):
        plugin = resolve_regime_plugin("legacy")
        assert isinstance(plugin, RegimeClassifierAdapter)

    def test_unknown_name_defaults_to_extended(self):
        plugin = resolve_regime_plugin("something_unknown")
        assert isinstance(plugin, ExtendedRegimePlugin)


# ---------------------------------------------------------------------------
# StageBus enforcement
# ---------------------------------------------------------------------------


class TestStageBusEnforcement:
    def _bus(self, plugin_id="my_plugin", plugin_version="1.0") -> StageBus:
        mock = MockEventBus()
        return StageBus(
            mock,
            StageBusConfig(
                run_id="r1",
                parity_mode=ParityMode.LIVE_FULL,
                plugin_id=plugin_id,
                plugin_version=plugin_version,
            ),
        )

    def test_valid_context_publishes_without_error(self):
        bus = self._bus()
        bus.publish_decision("stream:test:sim:r1", {"event_type": "test"})
        assert len(bus._bus.published) == 1

    def test_empty_plugin_id_raises_value_error(self):
        bus = self._bus(plugin_id="")
        with pytest.raises(ValueError, match="plugin_id"):
            bus.publish_decision("stream:test:sim:r1", {"event_type": "test"})

    def test_empty_plugin_version_raises_value_error(self):
        bus = self._bus(plugin_version="")
        with pytest.raises(ValueError, match="plugin_version"):
            bus.publish_decision("stream:test:sim:r1", {"event_type": "test"})

    def test_event_level_plugin_id_overrides_bus_default(self):
        bus = self._bus(plugin_id="bus_default")
        bus.publish_decision("stream:test:sim:r1", {"event_type": "test", "plugin_id": "event_plugin"})
        _, event = bus._bus.published[0]
        assert event["plugin_id"] == "event_plugin"

    def test_set_plugin_then_publish_passes(self):
        bus = self._bus(plugin_id="", plugin_version="")
        bus.set_plugin("new_plugin", "2.0")
        bus.publish_decision("stream:test:sim:r1", {"event_type": "test"})
        assert len(bus._bus.published) == 1

    def test_empty_parity_mode_raises_value_error(self):
        mock = MockEventBus()
        bus = StageBus(
            mock,
            StageBusConfig(run_id="r1", parity_mode=ParityMode.LIVE_FULL, plugin_id="p", plugin_version="1"),
        )
        # Manually corrupt stamped event to have empty parity_mode
        with pytest.raises(ValueError, match="parity_mode"):
            bus.publish_decision("stream:test:sim:r1", {"event_type": "test", "parity_mode": ""})
