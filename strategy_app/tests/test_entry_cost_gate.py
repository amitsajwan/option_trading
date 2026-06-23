"""Tests for the cost-ratio entry gate (arm B) and feature_health diagnostic."""
from __future__ import annotations

import pytest

from strategy_app.diagnostics import feature_health
from strategy_app.engines.strategies.entry_cost_gate import evaluate_cost_gate


class _Snap:
    def __init__(self, atr_ratio, spot, premium):
        self.raw_payload = {"futures_derived": {"atr_ratio": atr_ratio}}
        self.fut_close = spot
        self.atm_premium = premium


def test_cost_gate_passes_on_big_expected_move():
    r = evaluate_cost_gate(_Snap(0.00046, 58000, 500))
    assert r.ok is True
    assert r.evidence["cost_ratio"] >= 1.5
    assert r.evidence["expected_move_pt"] > 60


def test_cost_gate_drops_low_vol_bar():
    # ~37pt expected move can't clear the ~1.3% all-in cost wall.
    r = evaluate_cost_gate(_Snap(0.0002, 58000, 500))
    assert r.ok is False
    assert r.evidence["cost_ratio"] < 1.5


def test_cost_gate_failsafe_on_missing_inputs():
    # Missing atr_ratio / spot / premium must PASS (never silently block trading).
    assert evaluate_cost_gate(_Snap(None, 58000, 500)).ok is True
    assert evaluate_cost_gate(_Snap(0.0004, None, 500)).ok is True
    assert evaluate_cost_gate(_Snap(0.0004, 58000, None)).ok is True


def test_cost_gate_disabled(monkeypatch):
    monkeypatch.setenv("ENTRY_COST_RATIO_GATE_ENABLED", "0")
    r = evaluate_cost_gate(_Snap(0.0002, 58000, 500))  # would normally drop
    assert r.ok is True
    assert r.reason == "disabled"


def test_cost_gate_threshold_tunable(monkeypatch):
    monkeypatch.setenv("ENTRY_COST_RATIO_MIN", "5.0")
    r = evaluate_cost_gate(_Snap(0.00046, 58000, 500))  # ratio ~2.3 < 5.0
    assert r.ok is False


def test_feature_health_all_present():
    snap = {
        "snapshot_id": "T1",
        "futures_bar": {"fut_close": 57000},
        "futures_derived": {"fut_return_5m": 0.1, "price_vs_vwap": 0.0009, "ema_order": 0,
                            "atr_ratio": 1.2, "compression_score": 0.5, "adx_14": 22, "vol_spike_ratio": 1.1},
        "opening_range": {"orh": 57100},
        "chain_aggregates": {"pcr": 0.73, "pcr_change_5m": 0.01, "max_pain": 57600,
                             "total_ce_oi": 100, "total_ce_volume": 200},
        "atm_options": {"atm_ce_close": 512, "atm_ce_oi": 1, "atm_ce_volume": 1, "atm_ce_iv": 0.1},
        "strikes": [{"strike": 57000}] * 25,
        "vix_context": {"vix_current": 12.8, "vix_intraday_chg": -3.6},
    }
    r = feature_health(snap)
    assert r["degraded"] is False
    assert r["required_present"] == r["required_total"]


def test_feature_health_flags_missing_vix():
    # vix_intraday_chg in the WRONG place (the bug we fixed) → flagged missing.
    snap = {"snapshot_id": "T2", "vix_context": {"vix_current": 12.8}}
    r = feature_health(snap)
    assert r["degraded"] is True
    assert "vix_intraday_chg" in r["missing_required"]


def test_feature_health_depth_optional_not_degrading():
    # Depth absent must NOT mark the system degraded (optional until 2026-06-23).
    snap = {
        "snapshot_id": "T3",
        "futures_bar": {"fut_close": 57000},
        "futures_derived": {"fut_return_5m": 0.1, "price_vs_vwap": 0.0009, "ema_order": 0,
                            "atr_ratio": 1.2, "compression_score": 0.5, "adx_14": 22, "vol_spike_ratio": 1.1},
        "opening_range": {"orh": 57100},
        "chain_aggregates": {"pcr": 0.73, "pcr_change_5m": 0.01, "max_pain": 57600,
                             "total_ce_oi": 100, "total_ce_volume": 200},
        "atm_options": {"atm_ce_close": 512, "atm_ce_oi": 1, "atm_ce_volume": 1, "atm_ce_iv": 0.1},
        "strikes": [{"strike": 57000}] * 25,
        "vix_context": {"vix_current": 12.8, "vix_intraday_chg": -3.6},
    }
    r = feature_health(snap)
    assert r["degraded"] is False
    assert r["groups"]["option_depth_bid"]["present"] is False
    assert r["groups"]["option_depth_bid"]["required"] is False


def test_cost_gate_uses_depth_slippage(monkeypatch):
    """When depth is available, cost gate uses measured spread, not the flat placeholder."""
    from strategy_app.engines.strategies import entry_cost_gate as ecg
    from strategy_app.market.depth_context import DepthContext, StrikeDepth
    # ATM CE/PE with ~1% relative spread (bid 100, ask 101 -> 0.01)
    ce = StrikeDepth(best_bid=100.0, best_ask=101.0, bid_qty=1, ask_qty=1)
    pe = StrikeDepth(best_bid=100.0, best_ask=101.0, bid_qty=1, ask_qty=1)
    monkeypatch.setattr(ecg, "get_depth_context", lambda: DepthContext(ce=ce, pe=pe), raising=False)
    # patch the late import target too
    import strategy_app.runtime.eval_context as ev
    monkeypatch.setattr(ev, "get_depth_context", lambda: DepthContext(ce=ce, pe=pe))
    r = evaluate_cost_gate(_Snap(0.00046, 58000, 500))
    assert r.evidence.get("slippage_source") == "depth_measured"
    assert abs(r.evidence.get("slippage_pct") - 0.01) < 0.002  # ~1% measured


def test_cost_gate_flat_when_no_depth(monkeypatch):
    import strategy_app.runtime.eval_context as ev
    monkeypatch.setattr(ev, "get_depth_context", lambda: None)
    r = evaluate_cost_gate(_Snap(0.00046, 58000, 500))
    assert r.evidence.get("slippage_source") == "flat_placeholder"
