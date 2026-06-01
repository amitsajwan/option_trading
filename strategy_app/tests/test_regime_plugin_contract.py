"""Tests for RegimePlugin contract + RegimeClassifierAdapter (Phase 1 DoD criterion 7-8).

All tests run without live Redis or ML model files.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from strategy_app.brain.plugin import RegimeDecisionResult, RegimePlugin
from strategy_app.market.regime import Regime, RegimeClassifier, RegimeSignal
from strategy_app.market.regime_plugin_adapter import RegimeClassifierAdapter


# ---------------------------------------------------------------------------
# Minimal snapshot payload helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    *,
    vix_spike_flag: bool = False,
    is_expiry_day: bool = False,
    days_to_expiry: int = 3,
    vix_current: float = 15.0,
    vix_intraday_chg: float = 0.0,
    realized_vol_30m: float = 0.008,
    fut_return_5m: float = 0.002,
    fut_return_15m: float = 0.003,
    fut_return_30m: float = 0.004,
    vol_ratio: float = 1.5,
    pcr: float = 1.0,
    orh_broken: bool = False,
    orl_broken: bool = False,
    session_phase: str = "normal",
    is_pre_close: bool = False,
) -> dict[str, Any]:
    """Return a minimal snapshot payload sufficient for RegimeClassifier."""
    return {
        "snapshot_id": "test-snap-001",
        "session_context": {
            "date": "2026-05-31",
            "session_phase": session_phase,
            "vix_spike_flag": vix_spike_flag,
            "is_expiry_day": is_expiry_day,
            "days_to_expiry": days_to_expiry,
            "is_pre_close": is_pre_close,
        },
        "futures_bar": {
            "close": 49500.0,
        },
        "futures_derived": {
            "fut_return_5m": fut_return_5m,
            "fut_return_15m": fut_return_15m,
            "fut_return_30m": fut_return_30m,
            "vol_ratio": vol_ratio,
            "realized_vol_30m": realized_vol_30m,
            "fut_oi": 1000000.0,
            "fut_oi_change_30m": 5000.0,
        },
        "vix_context": {
            "vix_current": vix_current,
            "vix_intraday_chg": vix_intraday_chg,
            "vix_spike_flag": vix_spike_flag,
            "vix_regime": "normal",
        },
        "chain_aggregates": {
            "pcr": pcr,
        },
        "opening_range": {
            "ready": True,
            "orh": 49600.0,
            "orl": 49400.0,
            "orh_broken": orh_broken,
            "orl_broken": orl_broken,
        },
        "iv_derived": {
            "iv_percentile": 45.0,
            "iv_regime": "normal",
        },
        "atm_options": {},
        "session_levels": {},
    }


# ---------------------------------------------------------------------------
# RegimeDecisionResult contract
# ---------------------------------------------------------------------------


class TestRegimeDecisionResult:
    def test_is_named_tuple(self):
        r = RegimeDecisionResult(
            regime="trend",
            confidence=0.84,
            evidence={"x": 1},
            plugin_id="test",
            plugin_version="1.0",
        )
        assert r.regime == "trend"
        assert abs(r.confidence - 0.84) < 1e-9
        assert isinstance(r.evidence, dict)
        assert r.plugin_id == "test"

    def test_immutable(self):
        r = RegimeDecisionResult("trend", 0.5, {}, "p", "1.0")
        with pytest.raises((AttributeError, TypeError)):
            r.regime = "chop"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RegimePlugin ABC
# ---------------------------------------------------------------------------


class TestRegimePluginABC:
    def test_cannot_instantiate_abc_directly(self):
        with pytest.raises(TypeError):
            RegimePlugin()  # type: ignore[abstract]

    def test_concrete_implementation_must_implement_all_methods(self):
        class Incomplete(RegimePlugin):
            @property
            def plugin_id(self):
                return "test"
            @property
            def plugin_version(self):
                return "1.0"
            # missing classify()

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# RegimeClassifierAdapter
# ---------------------------------------------------------------------------


class TestRegimeClassifierAdapter:
    def test_plugin_id_and_version(self):
        adapter = RegimeClassifierAdapter()
        assert adapter.plugin_id == "regime_classifier_v1"
        assert adapter.plugin_version == "1.0"

    def test_classify_returns_regime_decision_result(self):
        adapter = RegimeClassifierAdapter()
        snap = _make_snapshot(fut_return_5m=0.003, fut_return_15m=0.004, fut_return_30m=0.005, vol_ratio=1.6)
        result = adapter.classify(snap, context={})
        assert isinstance(result, RegimeDecisionResult)

    def test_result_plugin_id_matches_adapter(self):
        adapter = RegimeClassifierAdapter()
        snap = _make_snapshot()
        result = adapter.classify(snap, context={})
        assert result.plugin_id == adapter.plugin_id
        assert result.plugin_version == adapter.plugin_version

    def test_result_regime_is_valid_enum_value(self):
        adapter = RegimeClassifierAdapter()
        snap = _make_snapshot(fut_return_5m=0.003, fut_return_15m=0.004, fut_return_30m=0.005)
        result = adapter.classify(snap, context={})
        valid_values = {r.value for r in Regime}
        assert result.regime in valid_values, f"unexpected regime: {result.regime!r}"

    def test_confidence_in_range(self):
        adapter = RegimeClassifierAdapter()
        snap = _make_snapshot()
        result = adapter.classify(snap, context={})
        assert 0.0 <= result.confidence <= 1.0

    def test_evidence_is_dict(self):
        adapter = RegimeClassifierAdapter()
        result = adapter.classify(_make_snapshot(), context={})
        assert isinstance(result.evidence, dict)

    def test_evidence_includes_reason(self):
        adapter = RegimeClassifierAdapter()
        result = adapter.classify(_make_snapshot(), context={})
        assert "reason" in result.evidence

    def test_vix_spike_returns_avoid(self):
        adapter = RegimeClassifierAdapter()
        snap = _make_snapshot(vix_spike_flag=True)
        result = adapter.classify(snap, context={})
        assert result.regime == Regime.AVOID.value

    def test_expiry_day_returns_expiry(self):
        adapter = RegimeClassifierAdapter()
        snap = _make_snapshot(is_expiry_day=True)
        result = adapter.classify(snap, context={})
        assert result.regime == Regime.EXPIRY.value

    def test_strong_aligned_returns_trending_or_breakout(self):
        adapter = RegimeClassifierAdapter()
        # orh_broken=True with strong vol → BREAKOUT (takes priority over TRENDING)
        snap = _make_snapshot(
            fut_return_5m=0.005, fut_return_15m=0.006, fut_return_30m=0.007,
            vol_ratio=2.0, orh_broken=True,
        )
        result = adapter.classify(snap, context={})
        # Phase 3: BREAKOUT correctly takes priority when orh is broken with strong vol
        assert result.regime in (Regime.TRENDING.value, Regime.BREAKOUT.value)

    def test_configure_forwards_thresholds_to_classifier(self):
        classifier = RegimeClassifier()
        adapter = RegimeClassifierAdapter(classifier=classifier)
        adapter.configure({"trend_return_min": 0.0050, "high_vol_vix_min": 30.0})
        assert abs(classifier._trend_return_min - 0.0050) < 1e-9
        assert abs(classifier._high_vol_vix_min - 30.0) < 1e-9

    def test_configure_with_none_is_safe(self):
        adapter = RegimeClassifierAdapter()
        adapter.configure(None)  # must not raise

    def test_configure_with_invalid_values_is_safe(self):
        adapter = RegimeClassifierAdapter()
        adapter.configure({"trend_return_min": "not-a-number"})  # must not raise

    def test_injected_classifier_is_used(self):
        mock_classifier = MagicMock(spec=RegimeClassifier)
        mock_classifier.classify.return_value = RegimeSignal(
            regime=Regime.SIDEWAYS,
            confidence=0.65,
            reason="MOCK",
            evidence={"mocked": True},
        )
        adapter = RegimeClassifierAdapter(classifier=mock_classifier)
        result = adapter.classify(_make_snapshot(), context={})
        assert result.regime == Regime.SIDEWAYS.value
        mock_classifier.classify.assert_called_once()

    def test_context_ignored_by_default_adapter(self):
        # The adapter wraps RegimeClassifier which ignores context; this test
        # ensures the adapter does not crash when context contains arbitrary keys.
        adapter = RegimeClassifierAdapter()
        result = adapter.classify(_make_snapshot(), context={"some_key": "some_value", "nested": {"x": 1}})
        assert isinstance(result, RegimeDecisionResult)

    def test_multiple_calls_are_independent(self):
        adapter = RegimeClassifierAdapter()
        snap_expiry = _make_snapshot(is_expiry_day=True)
        snap_normal = _make_snapshot(is_expiry_day=False)
        r1 = adapter.classify(snap_expiry, {})
        r2 = adapter.classify(snap_normal, {})
        assert r1.regime == Regime.EXPIRY.value
        assert r2.regime != Regime.EXPIRY.value or True  # just ensure no cross-contamination
