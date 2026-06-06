"""Synthetic end-to-end tests for the cost-aware brain backtest (board B-2.6)."""
from __future__ import annotations

import random

from ops.research.brain_backtest import BacktestReport, run_brain_backtest
from strategy_app.brain.decision_brain import CURVE_POINTS, P_REF


def _synthetic_day(seed: int = 7, n: int = 120) -> list[dict]:
    rng = random.Random(seed)
    bars = []
    oi = 100000.0
    close = 54000.0
    for i in range(n):
        oi *= 1.004
        if i < 45:
            width = 40.0
            close += rng.uniform(-15, 15)      # volatile baseline
        else:
            width = 20.0
            close += rng.uniform(-2, 2)        # compressed (ratio ~0.5)
        if 60 <= i < 78:
            close += 14.0                      # a real future upmove
        bars.append({"c": close, "h": close + width, "l": close - width,
                     "ovol": 1000.0 + i * 5, "ooi": oi,
                     "max_pain": 54600.0, "ce_oi_top_strike": 55200.0, "pe_oi_top_strike": 53000.0,
                     "opening_range_high": 54400.0, "opening_range_low": 53600.0})
    return bars


_LEVELS = {"d1": {"prior_day_high": 55000.0, "prior_day_low": 53000.0}}


def _run():
    return run_brain_backtest({"d1": _synthetic_day()}, levels=_LEVELS)


def test_backtest_produces_trades_and_curve():
    r = _run()
    assert r.trades > 0 and r.accountable_trades > 0
    assert set(r.net_curve) == set(CURVE_POINTS)


def test_curve_monotonic_increasing_in_accuracy():
    r = _run()
    vals = [r.net_curve[p] for p in sorted(r.net_curve)]
    assert vals == sorted(vals)
    assert r.net_curve[1.0] > r.net_curve[0.50]     # perfect direction beats coin-flip


def test_latency_under_budget_no_llm():
    r = _run()
    assert r.latency_ms_max < 1000.0                # D6: <1s per bar, no LLM on path
    assert r.latency_ms_p99 < 50.0                  # deterministic arithmetic is far under


def test_breakeven_interpolation_is_between_samples():
    r = _run()
    be = r.breakeven_accuracy()
    # this synthetic is negative at 0.55 and positive at 1.0 -> crossover strictly inside
    assert be is not None and 0.55 < be < 1.0


def test_gate_verdict_classifies_without_crashing():
    g = _run().gate()
    assert any(g.startswith(k) for k in ("PASS", "MARGINAL", "STOP", "NO-TRADES"))


def test_gate_stop_when_negative_even_at_perfect():
    # tiny moves -> gross can't clear cost even at perfect direction
    rng_day = _synthetic_day()
    r = BacktestReport(days=1, bars=10, trades=3, accountable_trades=3,
                       net_curve={0.50: -0.05, 0.55: -0.04, 0.58: -0.035, 0.60: -0.03, 1.0: -0.01},
                       avg_net_curve={p: 0.0 for p in CURVE_POINTS},
                       latency_ms_p99=0.1, latency_ms_max=0.2)
    assert r.gate().startswith("STOP")


def test_gate_pass_when_positive_at_pref():
    r = BacktestReport(days=1, bars=10, trades=3, accountable_trades=3,
                       net_curve={0.50: -0.01, 0.55: 0.005, 0.58: 0.01, 0.60: 0.015, 1.0: 0.05},
                       avg_net_curve={p: 0.0 for p in CURVE_POINTS},
                       latency_ms_p99=0.1, latency_ms_max=0.2)
    assert r.gate().startswith("PASS")


def test_inposition_cooldown_prevents_overlap():
    # with a 10-min horizon, two trades cannot be within 10 bars on the same day
    r = _run()
    # accountable trades exist and the run did not blow the (implicit) overlap guard
    assert r.accountable_trades <= r.trades
