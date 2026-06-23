"""Tests for the 2026-06-21 direction-signal additions to multi_signal:
max_pain pin, OI walls, cross-family agreement, and the VIX-key fix.
"""
from __future__ import annotations

import pytest

from strategy_app.contracts import Direction
from strategy_app.engines.strategies.entry_direction_policy import resolve_direction_for_entry
from strategy_app.market.snapshot_accessor import SnapshotAccessor


def _base_payload(**fd_over):
    """A snapshot with all-neutral signals; tests flip one family at a time."""
    fd = {"price_vs_vwap": 0.0, "ema_order": 0}
    fd.update(fd_over)
    return {
        "snapshot_id": "T",
        "futures_bar": {"fut_close": 58000},
        "futures_derived": fd,
        "opening_range": {},
        "atm_options": {"atm_ce_close": 100, "atm_pe_close": 100},
        "chain_aggregates": {"atm_strike": 58000},
        "vix_context": {},
    }


def _resolve(payload, env, monkeypatch):
    monkeypatch.setenv("ML_ENTRY_DIRECTION_MODE", "multi_signal")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return resolve_direction_for_entry(SnapshotAccessor(payload))


def test_vix_signal_reads_vix_context(monkeypatch):
    # VIX falling ≥3% in vix_context → bullish +1.5 (was dead when read from fd).
    p = _base_payload()
    p["vix_context"]["vix_intraday_chg"] = -3.6
    _d, rs = _resolve(p, {"ENTRY_MULTI_SIGNAL_MIN": 1.0}, monkeypatch)
    assert "vix_falling" in rs["multi_signal_fired"]
    assert rs["multi_signal_score"] >= 1.5


def test_maxpain_above_spot_is_bullish(monkeypatch):
    # max_pain above spot → price pulled up → CE lean.
    p = _base_payload()
    p["chain_aggregates"]["max_pain"] = 58500  # above 58000 spot
    _d, rs = _resolve(p, {"ENTRY_MULTI_SIGNAL_MIN": 0.5, "ENTRY_MS_OIWALL_ENABLED": 0}, monkeypatch)
    assert "maxpain_above" in rs["multi_signal_fired"]
    assert rs["multi_signal_score"] > 0


def test_maxpain_below_spot_is_bearish(monkeypatch):
    p = _base_payload()
    p["chain_aggregates"]["max_pain"] = 57500  # below spot
    _d, rs = _resolve(p, {"ENTRY_MULTI_SIGNAL_MIN": 0.5, "ENTRY_MS_OIWALL_ENABLED": 0}, monkeypatch)
    assert "maxpain_below" in rs["multi_signal_fired"]
    assert rs["multi_signal_score"] < 0


def test_maxpain_pinned_is_neutral(monkeypatch):
    # spot essentially at pin → no signal (avoid noise).
    p = _base_payload()
    p["chain_aggregates"]["max_pain"] = 58010  # within 0.1%
    _d, rs = _resolve(p, {"ENTRY_MULTI_SIGNAL_MIN": 0.5, "ENTRY_MS_OIWALL_ENABLED": 0}, monkeypatch)
    assert "maxpain" not in rs["multi_signal_fired"]


def test_oi_support_wall_is_bullish(monkeypatch):
    # PE wall nearer than CE wall → near support → CE.
    p = _base_payload()
    p["chain_aggregates"]["pe_oi_top_strike"] = 57900  # 100 below
    p["chain_aggregates"]["ce_oi_top_strike"] = 58500  # 500 above
    _d, rs = _resolve(p, {"ENTRY_MULTI_SIGNAL_MIN": 0.5, "ENTRY_MS_MAXPAIN_ENABLED": 0}, monkeypatch)
    assert "oi_support" in rs["multi_signal_fired"]
    assert rs["multi_signal_score"] > 0


def test_oi_resistance_wall_is_bearish(monkeypatch):
    p = _base_payload()
    p["chain_aggregates"]["pe_oi_top_strike"] = 57500  # 500 below
    p["chain_aggregates"]["ce_oi_top_strike"] = 58100  # 100 above
    _d, rs = _resolve(p, {"ENTRY_MULTI_SIGNAL_MIN": 0.5, "ENTRY_MS_MAXPAIN_ENABLED": 0}, monkeypatch)
    assert "oi_resistance" in rs["multi_signal_fired"]
    assert rs["multi_signal_score"] < 0


def test_signals_can_be_disabled(monkeypatch):
    p = _base_payload()
    p["chain_aggregates"]["max_pain"] = 58500
    p["chain_aggregates"]["pe_oi_top_strike"] = 57900
    p["chain_aggregates"]["ce_oi_top_strike"] = 58500
    _d, rs = _resolve(p, {"ENTRY_MULTI_SIGNAL_MIN": 0.5,
                          "ENTRY_MS_MAXPAIN_ENABLED": 0, "ENTRY_MS_OIWALL_ENABLED": 0}, monkeypatch)
    assert "maxpain" not in rs["multi_signal_fired"]
    assert "oi_" not in rs["multi_signal_fired"]


def test_family_agreement_counted(monkeypatch):
    # price-action bullish (vwap+ema) + options-flow bullish (maxpain) → 2 families agree.
    p = _base_payload(price_vs_vwap=0.5, ema_order=1)
    p["chain_aggregates"]["max_pain"] = 58500
    _d, rs = _resolve(p, {"ENTRY_MULTI_SIGNAL_MIN": 0.5, "ENTRY_MS_OIWALL_ENABLED": 0}, monkeypatch)
    assert rs["ms_families_agree"] >= 2
    assert rs["ms_family_price_action"] == 1
    assert rs["ms_family_options_flow"] == 1


def test_min_families_gate_abstains(monkeypatch):
    # Only price-action fires (1 family); require 2 → abstain even if score passes.
    p = _base_payload(price_vs_vwap=0.5, ema_order=1)  # score +3 from price action alone
    d, rs = _resolve(p, {"ENTRY_MULTI_SIGNAL_MIN": 2.0, "ENTRY_MS_MIN_FAMILIES": 2,
                         "ENTRY_MS_MAXPAIN_ENABLED": 0, "ENTRY_MS_OIWALL_ENABLED": 0}, monkeypatch)
    assert d is None
    assert "families=" in rs["multi_signal_result"]
