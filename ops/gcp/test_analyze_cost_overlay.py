"""Tests for the cost overlay + bootstrap PF CI added to analyze_oos_validation_run.

Run with: pytest ops/gcp/test_analyze_cost_overlay.py
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


@pytest.fixture(autouse=True)
def _stub_pymongo(monkeypatch):
    """analyze_oos_validation_run imports pymongo at module load; stub it."""
    fake = types.ModuleType("pymongo")
    fake.MongoClient = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymongo", fake)
    yield


def _fresh_module(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delitem(sys.modules, "analyze_oos_validation_run", raising=False)
    import importlib
    return importlib.import_module("analyze_oos_validation_run")


def _build_row(m, pnl_pct: float, entry_prem: float = 200.0, direction: str = "CE") -> dict:
    exit_prem = entry_prem * (1.0 + pnl_pct)
    units = 1 * m.LOT_SIZE
    entry_value = entry_prem * units
    exit_value = exit_prem * units
    gross = exit_value - entry_value
    cost = m._trade_costs(entry_value, exit_value)
    net = gross - cost
    return {
        "date": "2024-05-01",
        "direction": direction,
        "exit_reason": "TRAILING_STOP" if pnl_pct > 0 else "TIME_STOP",
        "strategy": "ML_ENTRY",
        "pnl_pct": pnl_pct,
        "stop_pct": 0.20,
        "cap_pnl_pct": (pnl_pct * entry_value) / m.CAPITAL,
        "ml_prob": 0.7,
        "entry_premium": entry_prem,
        "exit_premium": exit_prem,
        "lots": 1,
        "cost_amt": cost,
        "net_pnl_pct": net / entry_value,
        "net_cap_pnl_pct": net / m.CAPITAL,
    }


def test_costs_match_offline_cost_model_math(monkeypatch):
    m = _fresh_module(
        monkeypatch,
        OOS_COST_BROKERAGE_PER_ORDER=20.0,
        OOS_COST_CHARGES_BPS=2.5,
        OOS_COST_SLIPPAGE_BPS=7.5,
    )
    # entry value 1500, exit value 1575: brokerage 40 + (3075 * 10bps) = 43.075
    assert m._trade_costs(1500.0, 1575.0) == pytest.approx(43.075, abs=1e-3)
    # zero values → only brokerage
    assert m._trade_costs(0.0, 0.0) == pytest.approx(40.0, abs=1e-6)


def test_net_pf_below_gross_with_default_costs(monkeypatch):
    m = _fresh_module(monkeypatch, OOS_BOOTSTRAP_ITERATIONS=200)
    rows = [_build_row(m, p) for p in [0.05, 0.05, 0.05, -0.03, -0.03, -0.03]]
    gross_pf = m.profit_factor(rows)
    net_pf = m.profit_factor(rows, key="net_cap_pnl_pct", sign_key="net_pnl_pct")
    assert gross_pf > net_pf > 0


def test_zero_cost_overlay_matches_gross(monkeypatch):
    m = _fresh_module(
        monkeypatch,
        OOS_COST_BROKERAGE_PER_ORDER=0.0,
        OOS_COST_CHARGES_BPS=0.0,
        OOS_COST_SLIPPAGE_BPS=0.0,
    )
    rows = [_build_row(m, p) for p in [0.05, -0.03, 0.04, -0.02]]
    gross_pf = m.profit_factor(rows)
    net_pf = m.profit_factor(rows, key="net_cap_pnl_pct", sign_key="net_pnl_pct")
    assert gross_pf == pytest.approx(net_pf, rel=1e-6)


def test_high_cost_overlay_flips_pf_below_one(monkeypatch):
    """Realistic ATM BN spread (100bps/side) on marginal book → net PF should crash."""
    m = _fresh_module(monkeypatch, OOS_COST_SLIPPAGE_BPS=100.0)
    rows = [_build_row(m, p) for p in [0.02, 0.02, 0.02, -0.02, -0.02, -0.02]]
    net_pf = m.profit_factor(rows, key="net_cap_pnl_pct", sign_key="net_pnl_pct")
    assert net_pf < 1.0, f"expected net PF < 1.0 at 100bps slippage, got {net_pf}"


def test_bootstrap_ci_bounds_contain_point_estimate(monkeypatch):
    m = _fresh_module(monkeypatch, OOS_BOOTSTRAP_ITERATIONS=500, OOS_BOOTSTRAP_SEED=7)
    rows = [_build_row(m, p) for p in [0.05] * 30 + [-0.03] * 30]
    point_pf = m.profit_factor(rows, key="net_cap_pnl_pct", sign_key="net_pnl_pct")
    lo, med, hi = m.bootstrap_pf_ci(rows, iterations=500, seed=7)
    assert lo <= point_pf <= hi, f"point {point_pf} outside CI [{lo}, {hi}]"
    assert hi > lo


def test_bootstrap_ci_seed_is_deterministic(monkeypatch):
    m = _fresh_module(monkeypatch)
    rows = [_build_row(m, p) for p in [0.05, -0.03, 0.04, -0.02, 0.03, -0.01]]
    a = m.bootstrap_pf_ci(rows, iterations=300, seed=123)
    b = m.bootstrap_pf_ci(rows, iterations=300, seed=123)
    assert a == b
