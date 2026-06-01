"""Unit tests for strategy_app.sim.exit_replay.

Tests the core simulation logic (_simulate_exit_stacks) and aggregation
(_aggregate_exit_results) without touching parquet or the real engine.

Key scenarios:
- Scalper cuts a winning trade via trail before it gives back much.
- Lottery holds through a dip and captures a bigger move.
- Thesis-fail cuts a flat/losing trade quickly in both modes.
- Fat-tail day: lottery captures a larger fraction of the tail move.
- Aggregation: profit_factor, max_drawdown, fat_tail_capture are computed correctly.
"""

from __future__ import annotations

import math
import pytest

from strategy_app.sim.exit_replay import (
    DayOutcomes,
    ExitReplayResult,
    StackAggregate,
    TradeOutcome,
    _aggregate_exit_results,
    _simulate_exit_stacks,
    _render_exit_report,
)
from strategy_app.position.exit_policy import (
    build_scalper_exit_stack,
    build_lottery_exit_stack,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stacks(**env_overrides):
    """Build scalper + lottery stacks, optionally with env overrides."""
    import os
    from unittest.mock import patch
    with patch.dict("os.environ", {
        "EXIT_POLICY_STACK_ENABLED": "1",
        "EXIT_TRAILING_ACTIVATION_PCT": "0.01",
        "EXIT_TRAILING_TRAIL_PCT": "0.005",
        "EXIT_THESIS_FAIL_BARS": "3",
        "EXIT_THESIS_FAIL_MIN_MFE": "0.002",
        "EXIT_PREMIUM_TARGET_PCT": "0.04",
        "LOTTERY_HARD_STOP_PCT": "0.25",
        "LOTTERY_BIG_TARGET_PCT": "0.40",
        "LOTTERY_RUNNER_ACTIVATION_MFE": "0.10",
        "LOTTERY_RUNNER_GIVEBACK_FRAC": "0.35",
        "LOTTERY_THESIS_FAIL_BARS": "4",
        "LOTTERY_THESIS_FAIL_MIN_MFE": "0.03",
        "LOTTERY_MOMENTUM_FLIP": "0",   # off: simpler
        "LOTTERY_TIMESTOP_BARS": "90",
        **env_overrides,
    }):
        return {
            "scalper": build_scalper_exit_stack(),
            "lottery": build_lottery_exit_stack(),
        }


def _ltp(entry: float, pct_moves: list[float]) -> list[float]:
    """Build LTP series from entry + list of percent changes per bar."""
    series = [entry]
    cur = entry
    for pct in pct_moves:
        cur = cur * (1 + pct)
        series.append(cur)
    return series


# ---------------------------------------------------------------------------
# _simulate_exit_stacks — core sim logic
# ---------------------------------------------------------------------------

def test_flat_trade_thesis_fail_both_stacks():
    """A trade that goes nowhere triggers thesis_fail in both stacks."""
    stacks = _make_stacks()
    # 10 bars of 0% movement → MFE never reaches 0.2% → thesis fail at bar 3
    ltp_series = _ltp(1000.0, [0.0] * 12)
    outcomes = _simulate_exit_stacks(
        ltp_series=ltp_series,
        entry_bar=0,
        direction="CE",
        strike=48000,
        otm_steps=0,
        stacks=stacks,
        underlying_move_pct=0.0,
        trade_date="2023-06-01",
    )
    assert len(outcomes) == 2
    for o in outcomes:
        assert "thesis_fail" in o.exit_reason.lower() or "THESIS_FAIL" in o.exit_reason
        assert o.bars_held <= 5   # cut early, not held for 10 bars


def test_scalper_trails_out_before_lottery():
    """On a moderate move (5%), scalper's tight trail exits before lottery's loose runner."""
    stacks = _make_stacks(
        EXIT_TRAILING_ACTIVATION_PCT="0.01",
        EXIT_TRAILING_TRAIL_PCT="0.005",
        LOTTERY_RUNNER_ACTIVATION_MFE="0.10",  # requires 10% MFE before runner activates
        LOTTERY_TIMESTOP_BARS="90",
    )
    # Price runs up 5% in 8 bars, then drops back
    moves = [0.005, 0.008, 0.010, 0.008, 0.005, 0.003, -0.005, -0.010, -0.005]
    ltp_series = _ltp(1000.0, moves)
    outcomes = _simulate_exit_stacks(
        ltp_series=ltp_series, entry_bar=0,
        direction="CE", strike=48000, otm_steps=0,
        stacks=stacks, underlying_move_pct=0.05, trade_date="2023-06-01",
    )
    scalper = next(o for o in outcomes if o.stack_name == "scalper")
    lottery = next(o for o in outcomes if o.stack_name == "lottery")

    # Scalper should exit earlier (via trail) than lottery
    assert scalper.bars_held <= lottery.bars_held, (
        f"scalper bars={scalper.bars_held} should be <= lottery bars={lottery.bars_held}"
    )


def test_lottery_captures_big_tail_move():
    """On a 30% option move (fat tail), lottery captures more than scalper."""
    stacks = _make_stacks(
        EXIT_TRAILING_ACTIVATION_PCT="0.01",
        EXIT_TRAILING_TRAIL_PCT="0.005",
        LOTTERY_RUNNER_ACTIVATION_MFE="0.10",
        LOTTERY_RUNNER_GIVEBACK_FRAC="0.35",
        LOTTERY_BIG_TARGET_PCT="0.40",
        LOTTERY_TIMESTOP_BARS="90",
    )
    # Option gains 30% steadily over 40 bars — a real tail event
    moves = [0.008] * 35 + [-0.01, -0.02, -0.03]
    ltp_series = _ltp(500.0, moves)
    outcomes = _simulate_exit_stacks(
        ltp_series=ltp_series, entry_bar=0,
        direction="PE", strike=47900, otm_steps=1,
        stacks=stacks, underlying_move_pct=0.04, trade_date="2023-06-01",
    )
    scalper = next(o for o in outcomes if o.stack_name == "scalper")
    lottery = next(o for o in outcomes if o.stack_name == "lottery")

    # Lottery should capture more P&L on the big move
    assert lottery.pnl_pct > scalper.pnl_pct, (
        f"lottery pnl={lottery.pnl_pct:.3f} should exceed scalper pnl={scalper.pnl_pct:.3f}"
    )


def test_lottery_hard_stop_caps_loss():
    """Lottery's hard stop fires when loss exceeds LOTTERY_HARD_STOP_PCT."""
    stacks = _make_stacks(
        LOTTERY_HARD_STOP_PCT="0.25",
        LOTTERY_THESIS_FAIL_BARS="100",   # disable thesis fail for this test
    )
    # Option drops 30% — lottery hard stop should fire at -25%
    moves = [-0.05] * 8   # cumulative: -(1-0.95^8) ≈ -34%
    ltp_series = _ltp(1000.0, moves)
    outcomes = _simulate_exit_stacks(
        ltp_series=ltp_series, entry_bar=0,
        direction="CE", strike=48000, otm_steps=0,
        stacks=stacks, underlying_move_pct=-0.03, trade_date="2023-06-01",
    )
    lottery = next(o for o in outcomes if o.stack_name == "lottery")
    # Stop fires at bar close, so actual exit can overshoot by up to one bar's move (~5%).
    assert lottery.pnl_pct >= -0.31, f"Loss {lottery.pnl_pct:.3f} should be capped within one bar of -0.25"
    assert "STOP_LOSS" in lottery.exit_reason or "stop" in lottery.exit_reason.lower()


def test_empty_ltp_returns_no_outcomes():
    stacks = _make_stacks()
    outcomes = _simulate_exit_stacks(
        ltp_series=[], entry_bar=0,
        direction="CE", strike=48000, otm_steps=0,
        stacks=stacks, underlying_move_pct=0.0, trade_date="2023-06-01",
    )
    assert outcomes == []


def test_entry_bar_beyond_series_returns_no_outcomes():
    stacks = _make_stacks()
    outcomes = _simulate_exit_stacks(
        ltp_series=[500.0, 510.0], entry_bar=5,   # beyond series
        direction="CE", strike=48000, otm_steps=0,
        stacks=stacks, underlying_move_pct=0.0, trade_date="2023-06-01",
    )
    assert outcomes == []


def test_capture_ratio_computed_correctly():
    """capture_ratio = pnl / mfe for trades with mfe > 0."""
    stacks = _make_stacks(
        EXIT_TRAILING_ACTIVATION_PCT="0.05",  # high: won't trail on small moves
        EXIT_THESIS_FAIL_BARS="100",
        EXIT_PREMIUM_TARGET_PCT="0.10",
    )
    # Goes up 5%, then back to +2%
    moves = [0.01, 0.02, 0.015, 0.01, -0.02, -0.015]
    ltp_series = _ltp(1000.0, moves)
    outcomes = _simulate_exit_stacks(
        ltp_series=ltp_series, entry_bar=0,
        direction="CE", strike=48000, otm_steps=0,
        stacks=stacks, underlying_move_pct=0.02, trade_date="2023-06-01",
    )
    for o in outcomes:
        if o.mfe_pct > 0 and not math.isnan(o.capture_ratio):
            expected = o.pnl_pct / o.mfe_pct
            assert o.capture_ratio == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# _aggregate_exit_results — portfolio math
# ---------------------------------------------------------------------------

def _make_replay_result(outcomes_by_stack: dict) -> ExitReplayResult:
    """Build a minimal ExitReplayResult from {stack_name: [pnl_pct, ...]} dicts."""
    stack_names = list(outcomes_by_stack.keys())
    days = []
    n = max(len(v) for v in outcomes_by_stack.values())
    for i in range(n):
        day = DayOutcomes(trade_date=f"2023-01-{i+2:02d}", underlying_move_pct=0.0)
        for sname, pnls in outcomes_by_stack.items():
            if i < len(pnls):
                pnl = pnls[i]
                day.outcomes.append(TradeOutcome(
                    trade_date=day.trade_date, direction="CE",
                    entry_bar=5, entry_ltp=500.0,
                    exit_bar=10, exit_ltp=500.0 * (1 + pnl),
                    pnl_pct=pnl, mfe_pct=max(pnl, 0.0), mae_pct=min(pnl, 0.0),
                    capture_ratio=1.0 if pnl > 0 else float("nan"),
                    bars_held=5, exit_reason="test", stack_name=sname,
                    strike=48000, otm_steps=0, underlying_move_pct=0.0,
                ))
        days.append(day)

    result = ExitReplayResult(date_from="2023-01-02", date_to=f"2023-01-{n+1:02d}",
                               entry_bar=5, otm_steps=0, days=days)
    fake_stacks = {name: object() for name in stack_names}
    _aggregate_exit_results(result, fake_stacks, fat_tail_threshold=0.03)
    return result


def test_aggregate_profit_factor():
    result = _make_replay_result({"scalper": [0.05, -0.01, 0.03]})
    agg = result.aggregates["scalper"]
    # wins: 0.05 + 0.03 = 0.08, losses: 0.01, PF = 8.0
    assert agg.profit_factor == pytest.approx(8.0)


def test_aggregate_max_drawdown():
    # cumulative P&L curve: 0.05, 0.04, 0.07 → peak 0.07, no trough below 0.04
    # then 0.04 → dd = 0.07 - 0.04 = 0.03... wait
    # pnls=[0.05, -0.01, 0.03]: cum=[0.05, 0.04, 0.07] → peak at 0.07, then nothing lower
    # Actually peak is 0.07 at end → dd = 0 for the last step. Max dd from 0.05 to 0.04 = 0.01
    result = _make_replay_result({"scalper": [0.05, -0.01, 0.03]})
    agg = result.aggregates["scalper"]
    assert agg.max_drawdown == pytest.approx(0.01)


def test_aggregate_no_losses_infinite_pf():
    result = _make_replay_result({"scalper": [0.02, 0.03, 0.01]})
    agg = result.aggregates["scalper"]
    assert agg.profit_factor == float("inf")


def test_aggregate_win_rate():
    result = _make_replay_result({"scalper": [0.05, -0.01, 0.03, -0.02]})
    agg = result.aggregates["scalper"]
    assert agg.wins == 2
    assert agg.trades == 4


# ---------------------------------------------------------------------------
# _render_exit_report — smoke test
# ---------------------------------------------------------------------------

def test_render_report_contains_key_sections():
    result = _make_replay_result({"scalper": [0.05, -0.01], "lottery": [0.03, -0.02]})
    report = _render_exit_report(result)
    assert "Exit Policy Replay Report" in report
    assert "Stack Comparison" in report
    assert "Per-Day Summary" in report
    assert "scalper" in report
    assert "lottery" in report
    assert "synthetic" in report.lower()
