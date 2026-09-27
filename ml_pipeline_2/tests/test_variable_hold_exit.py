"""Tests for the variable-hold, asymmetric exit simulator
(ml_pipeline_2/src/ml_pipeline_2/pipeline/variable_hold_exit.py).

Fixture convention for SnapshotAccessor-based tests matches
test_option_payoff_labels.py's `_snap`/`_strike_row` helpers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.variable_hold_exit import (
    EXIT_REASON_HARD_STOP,
    EXIT_REASON_TIME_CAP,
    EXIT_REASON_TRAILING_STOP,
    REASON_CODE,
    ExitGridParams,
    ForwardPath,
    assign_random_sides,
    build_exit_grid,
    build_forward_premium_path,
    entry_leg_info,
    hard_stop_whipsaw_report,
    pad_premium_paths,
    select_inputs_by_side,
    select_outcome_by_side,
    simulate_exit,
    simulate_exit_grid_batch,
    summarize_returns,
)
from strategy_app.cost_model import TradingCostModel
from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.senses.cost_ev import CostEvSense

LOT_SIZE = 65  # NIFTY
_COST = TradingCostModel()


def _snap(
    *,
    timestamp: str = "2026-01-05T04:15:00+00:00",  # 09:45 IST
    trade_date: str = "2026-01-05",
    atm_strike: int | None = 24500,
    atm_ce_close: float | None = 100.0,
    atm_pe_close: float | None = 100.0,
    atm_ce_oi: float | None = 50000.0,
    atm_pe_oi: float | None = 50000.0,
    strikes: list[dict] | None = None,
) -> SnapshotAccessor:
    payload: dict = {
        "snapshot_id": "s1",
        "timestamp": timestamp,
        "trade_date": trade_date,
        "session_context": {"date": trade_date, "time": timestamp},
        "chain_aggregates": {"atm_strike": atm_strike},
        "atm_options": {
            "atm_ce_close": atm_ce_close,
            "atm_pe_close": atm_pe_close,
            "atm_ce_oi": atm_ce_oi,
            "atm_pe_oi": atm_pe_oi,
        },
        "strikes": strikes or [],
    }
    return SnapshotAccessor(payload)


def _strike_row(strike: int, *, ce_ltp: float | None, pe_ltp: float | None) -> dict:
    return {"strike": strike, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp}


def _day_snaps(ce_prices: list[float | None], *, strike: int = 24500, start_hhmm: str = "09:45") -> list[SnapshotAccessor]:
    """One bar per entry in `ce_prices` (1-minute cadence, IST), all quoting
    the SAME fixed strike -- pe_ltp mirrored as 200 - ce_ltp for simplicity
    (not used in most of these tests)."""
    start = pd.Timestamp(f"2026-01-05 {start_hhmm}")
    out = []
    for i, ce in enumerate(ce_prices):
        ts_ist = start + pd.Timedelta(minutes=i)
        ts_utc = ts_ist - pd.Timedelta(hours=5, minutes=30)
        pe = None if ce is None else 200.0 - ce
        out.append(
            _snap(
                timestamp=ts_utc.isoformat() + "+00:00",
                atm_strike=strike,
                strikes=[_strike_row(strike, ce_ltp=ce, pe_ltp=pe)],
            )
        )
    return out


class TestExitGridParams:
    def test_valid_construction(self) -> None:
        p = ExitGridParams(stop_loss_pct=0.05, trail_activate_pct=0.10, trail_distance_pct=0.20)
        assert p.stop_loss_pct == 0.05
        assert "stop0.05" in p.label

    @pytest.mark.parametrize("kwargs", [
        {"stop_loss_pct": 0.0}, {"stop_loss_pct": -0.1},
        {"trail_activate_pct": 0.0}, {"trail_activate_pct": -0.1},
        {"trail_distance_pct": 0.0}, {"trail_distance_pct": 1.0}, {"trail_distance_pct": 1.5},
    ])
    def test_rejects_invalid_params(self, kwargs: dict) -> None:
        base = dict(stop_loss_pct=0.05, trail_activate_pct=0.10, trail_distance_pct=0.20)
        base.update(kwargs)
        with pytest.raises(ValueError):
            ExitGridParams(**base)

    def test_build_exit_grid_is_cartesian_product(self) -> None:
        grid = build_exit_grid([0.05, 0.10], [0.10, 0.20], [0.30])
        assert len(grid) == 2 * 2 * 1
        combos = {(g.stop_loss_pct, g.trail_activate_pct, g.trail_distance_pct) for g in grid}
        assert (0.05, 0.10, 0.30) in combos
        assert (0.10, 0.20, 0.30) in combos


class TestSimulateExitScalar:
    def test_hard_stop_fires_at_exact_bar(self) -> None:
        params = ExitGridParams(stop_loss_pct=0.15, trail_activate_pct=0.50, trail_distance_pct=0.30)
        outcome = simulate_exit(100.0, [100.0, 95.0, 80.0, 110.0], params, cost_pct=0.0)
        assert outcome.exit_bar_offset == 3
        assert outcome.exit_reason == EXIT_REASON_HARD_STOP
        assert outcome.exit_premium == 80.0
        assert outcome.gross_return_pct == pytest.approx(-0.20)

    def test_trailing_stop_fires_at_exact_bar(self) -> None:
        params = ExitGridParams(stop_loss_pct=0.15, trail_activate_pct=0.10, trail_distance_pct=0.20)
        path = [100.0, 105.0, 112.0, 130.0, 140.0, 120.0, 100.0]
        outcome = simulate_exit(100.0, path, params, cost_pct=0.0)
        assert outcome.exit_bar_offset == 7
        assert outcome.exit_reason == EXIT_REASON_TRAILING_STOP
        assert outcome.exit_premium == 100.0
        assert outcome.peak_premium == 140.0

    def test_trailing_activation_alone_does_not_exit(self) -> None:
        """Crossing the activation threshold just arms the trailing stop --
        it must not itself be an exit event."""
        params = ExitGridParams(stop_loss_pct=0.50, trail_activate_pct=0.10, trail_distance_pct=0.20)
        path = [112.0]  # +12%, activates trailing, but 112 <= 112*0.8 is False
        outcome = simulate_exit(100.0, path, params, cost_pct=0.0)
        assert outcome.exit_reason == EXIT_REASON_TIME_CAP

    def test_time_cap_when_neither_rule_fires(self) -> None:
        params = ExitGridParams(stop_loss_pct=0.50, trail_activate_pct=0.50, trail_distance_pct=0.50)
        path = [101.0, 102.0, 103.0, 104.0, 105.0]
        outcome = simulate_exit(100.0, path, params, cost_pct=0.0)
        assert outcome.exit_bar_offset == 5
        assert outcome.exit_reason == EXIT_REASON_TIME_CAP
        assert outcome.exit_premium == 105.0

    def test_cost_is_subtracted_from_gross(self) -> None:
        params = ExitGridParams(stop_loss_pct=0.50, trail_activate_pct=0.50, trail_distance_pct=0.50)
        outcome = simulate_exit(100.0, [110.0], params, cost_pct=0.02)
        assert outcome.gross_return_pct == pytest.approx(0.10)
        assert outcome.net_return_pct == pytest.approx(0.08)

    def test_hard_stop_takes_priority_over_trailing_on_same_bar(self) -> None:
        """A bar that would satisfy BOTH an already-active trailing stop AND
        the hard-stop floor must be reported as a hard stop -- the absolute
        floor wins over the looser secondary rule."""
        params = ExitGridParams(stop_loss_pct=0.05, trail_activate_pct=0.10, trail_distance_pct=0.50)
        path = [115.0, 50.0]
        # bar1: +15% activates trailing (peak=115, trail_level=57.5); no exit (115 > 57.5)
        # bar2: price=50 is BOTH <= entry*(1-0.05)=95 (hard stop) AND <= 57.5 (trailing)
        outcome = simulate_exit(100.0, path, params, cost_pct=0.0)
        assert outcome.exit_reason == EXIT_REASON_HARD_STOP
        assert outcome.exit_bar_offset == 2
        assert outcome.exit_premium == 50.0

    def test_empty_path_raises(self) -> None:
        params = ExitGridParams(stop_loss_pct=0.10, trail_activate_pct=0.10, trail_distance_pct=0.20)
        with pytest.raises(ValueError):
            simulate_exit(100.0, [], params, cost_pct=0.0)

    def test_nonpositive_entry_premium_raises(self) -> None:
        params = ExitGridParams(stop_loss_pct=0.10, trail_activate_pct=0.10, trail_distance_pct=0.20)
        with pytest.raises(ValueError):
            simulate_exit(0.0, [100.0], params, cost_pct=0.0)


class TestSimulateExitGridBatchConsistency:
    """The vectorized batch implementation must agree with the scalar
    reference implementation bar-for-bar, entry-for-entry -- an off-by-one
    here would silently corrupt every downstream number."""

    def test_batch_matches_scalar_across_entries_and_combos(self) -> None:
        grid = build_exit_grid([0.05, 0.10, 0.20], [0.10, 0.20], [0.20, 0.40])
        raw_paths = [
            [100.0, 95.0, 80.0, 110.0, 120.0],          # hits hard stop at low stop levels
            [100.0, 105.0, 112.0, 130.0, 140.0, 120.0, 100.0],  # trailing-stop case, longer than others
            [101.0, 102.0, 99.0, 103.0, 104.0],          # time-cap case, nothing fires
            [100.0, 60.0],                                # short path, immediate hard stop everywhere
        ]
        entry_premiums = np.array([100.0, 100.0, 100.0, 100.0])
        cost_pcts = np.array([0.013, 0.013, 0.013, 0.013])
        forward_paths = [ForwardPath(premiums=np.asarray(p), n_real=len(p)) for p in raw_paths]
        max_bars = max(len(p) for p in raw_paths)
        padded, lens = pad_premium_paths(forward_paths, max_bars)

        batch = simulate_exit_grid_batch(entry_premiums, padded, lens, cost_pcts, grid)

        for i, raw in enumerate(raw_paths):
            for j, params in enumerate(grid):
                ref = simulate_exit(entry_premiums[i], raw, params, cost_pct=cost_pcts[i])
                assert batch.exit_bar_offset[i, j] == ref.exit_bar_offset, (i, j, params)
                assert REASON_CODE[ref.exit_reason] == batch.exit_reason_code[i, j], (i, j, params)
                assert batch.exit_premium[i, j] == pytest.approx(ref.exit_premium), (i, j, params)
                assert batch.net_return_pct[i, j] == pytest.approx(ref.net_return_pct, abs=1e-6), (i, j, params)

    def test_invalid_leg_row_propagates_nan_without_crashing(self) -> None:
        grid = build_exit_grid([0.10], [0.20], [0.30])
        entry_premiums = np.array([100.0, np.nan])
        forward_paths = [
            ForwardPath(premiums=np.asarray([101.0, 102.0]), n_real=2),
            ForwardPath(premiums=np.asarray([]), n_real=0),
        ]
        padded, lens = pad_premium_paths(forward_paths, 2)
        cost_pcts = np.array([0.01, np.nan])
        batch = simulate_exit_grid_batch(entry_premiums, padded, lens, cost_pcts, grid)
        assert np.isnan(batch.net_return_pct[1, 0])
        assert not np.isnan(batch.net_return_pct[0, 0])

    def test_zero_width_paths_raise(self) -> None:
        grid = build_exit_grid([0.10], [0.20], [0.30])
        entry_premiums = np.array([100.0])
        empty_paths = np.zeros((1, 0))
        with pytest.raises(ValueError):
            simulate_exit_grid_batch(entry_premiums, empty_paths, np.array([0]), np.array([0.01]), grid)


class TestPadPremiumPaths:
    def test_forward_fills_short_paths_with_last_value(self) -> None:
        paths = [
            ForwardPath(premiums=np.asarray([1.0, 2.0, 3.0]), n_real=3),
            ForwardPath(premiums=np.asarray([5.0]), n_real=1),
        ]
        padded, lens = pad_premium_paths(paths, 4)
        assert lens.tolist() == [3, 1]
        assert padded[0].tolist() == [1.0, 2.0, 3.0, 3.0]
        assert padded[1].tolist() == [5.0, 5.0, 5.0, 5.0]

    def test_zero_length_path_is_all_nan(self) -> None:
        paths = [ForwardPath(premiums=np.asarray([]), n_real=0)]
        padded, lens = pad_premium_paths(paths, 3)
        assert lens.tolist() == [0]
        assert np.isnan(padded[0]).all()


class TestBuildForwardPremiumPath:
    def test_forward_path_from_entry_plus_one(self) -> None:
        snaps = _day_snaps([100.0, 101.0, 103.0, 106.0, 110.0])
        fp = build_forward_premium_path(snaps, 0, "CE", 24500, 100.0, max_bars=10)
        assert fp.n_real == 4
        assert fp.premiums.tolist() == [101.0, 103.0, 106.0, 110.0]

    def test_respects_max_bars_cap(self) -> None:
        snaps = _day_snaps([100.0 + i for i in range(20)])
        fp = build_forward_premium_path(snaps, 0, "CE", 24500, 100.0, max_bars=3)
        assert fp.n_real == 3
        assert fp.premiums.tolist() == [101.0, 102.0, 103.0]

    def test_respects_session_end_cutoff(self) -> None:
        # Start at 14:50 IST -> 20 one-minute bars runs to 15:09 IST, past
        # the 15:05 session-end convention.
        snaps = _day_snaps([100.0 + i for i in range(20)], start_hhmm="14:50")
        fp = build_forward_premium_path(snaps, 0, "CE", 24500, 100.0, max_bars=90, session_end_min=15 * 60 + 5)
        # entry at bar0 (14:50); forward bars run 14:51..15:05 inclusive = 15 bars
        assert fp.n_real == 15
        assert fp.premiums[-1] == 100.0 + 15  # bar index 15 -> price 115 at 15:05

    def test_missing_quote_is_forward_filled_from_last_known_price(self) -> None:
        snaps = _day_snaps([100.0, 105.0, None, None, 112.0])
        fp = build_forward_premium_path(snaps, 0, "CE", 24500, 100.0, max_bars=10)
        assert fp.n_real == 4
        # bar1=105 (real), bar2/3 forward-filled from 105, bar4=112 (real)
        assert fp.premiums.tolist() == [105.0, 105.0, 105.0, 112.0]

    def test_leading_missing_quote_forward_filled_from_entry_premium(self) -> None:
        snaps = _day_snaps([100.0, None, 108.0])
        fp = build_forward_premium_path(snaps, 0, "CE", 24500, 100.0, max_bars=10)
        assert fp.premiums.tolist() == [100.0, 108.0]

    def test_no_forward_bars_returns_empty(self) -> None:
        snaps = _day_snaps([100.0])
        fp = build_forward_premium_path(snaps, 0, "CE", 24500, 100.0, max_bars=10)
        assert fp.n_real == 0
        assert fp.premiums.size == 0


class TestEntryLegInfo:
    def test_valid_ce_leg(self) -> None:
        entry = _snap(atm_strike=24500, atm_ce_close=150.0, atm_ce_oi=50000.0)
        info = entry_leg_info(entry, "CE", lot_size=LOT_SIZE, cost_model=_COST)
        assert info is not None
        assert info.strike == 24500
        assert info.entry_premium == 150.0
        expected_cost = CostEvSense(premium_pts=150.0, lot_qty=LOT_SIZE, cost_model=_COST).cost_pct()
        assert info.cost_pct == pytest.approx(expected_cost)

    def test_valid_pe_leg(self) -> None:
        entry = _snap(atm_strike=24500, atm_pe_close=120.0, atm_pe_oi=50000.0)
        info = entry_leg_info(entry, "PE", lot_size=LOT_SIZE, cost_model=_COST)
        assert info is not None
        assert info.entry_premium == 120.0

    def test_missing_atm_strike_returns_none(self) -> None:
        entry = _snap(atm_strike=None)
        assert entry_leg_info(entry, "CE", lot_size=LOT_SIZE) is None

    def test_thin_oi_rejects_leg(self) -> None:
        from ml_pipeline_2.pipeline.option_payoff_labels import MIN_ENTRY_OI
        entry = _snap(atm_strike=24500, atm_ce_close=150.0, atm_ce_oi=MIN_ENTRY_OI - 1)
        assert entry_leg_info(entry, "CE", lot_size=LOT_SIZE) is None

    def test_tiny_premium_rejects_leg(self) -> None:
        from ml_pipeline_2.pipeline.option_payoff_labels import MIN_ENTRY_PREMIUM
        entry = _snap(atm_strike=24500, atm_ce_close=MIN_ENTRY_PREMIUM - 1)
        assert entry_leg_info(entry, "CE", lot_size=LOT_SIZE) is None

    def test_invalid_side_raises(self) -> None:
        entry = _snap(atm_strike=24500)
        with pytest.raises(ValueError):
            entry_leg_info(entry, "XX", lot_size=LOT_SIZE)


class TestAssignRandomSides:
    def test_same_seed_is_reproducible(self) -> None:
        a = assign_random_sides(500, seed=7)
        b = assign_random_sides(500, seed=7)
        assert (a == b).all()

    def test_different_seeds_differ(self) -> None:
        a = assign_random_sides(500, seed=7)
        b = assign_random_sides(500, seed=8)
        assert not (a == b).all()

    def test_roughly_balanced(self) -> None:
        a = assign_random_sides(20000, seed=1)
        frac_ce = a.mean()
        assert 0.47 < frac_ce < 0.53


class TestSelectOutcomeBySide:
    def test_selects_ce_or_pe_row_by_mask(self) -> None:
        from ml_pipeline_2.pipeline.variable_hold_exit import BatchExitResult

        ce = BatchExitResult(
            exit_bar_offset=np.array([[1]]), exit_reason_code=np.array([[0]]),
            exit_premium=np.array([[80.0]]), gross_return_pct=np.array([[-0.2]]), net_return_pct=np.array([[-0.21]]),
        )
        pe = BatchExitResult(
            exit_bar_offset=np.array([[3]]), exit_reason_code=np.array([[2]]),
            exit_premium=np.array([[130.0]]), gross_return_pct=np.array([[0.3]]), net_return_pct=np.array([[0.29]]),
        )
        is_ce = np.array([True])
        selected = select_outcome_by_side(is_ce, ce, pe)
        assert selected.net_return_pct[0, 0] == pytest.approx(-0.21)

        is_ce2 = np.array([False])
        selected2 = select_outcome_by_side(is_ce2, ce, pe)
        assert selected2.net_return_pct[0, 0] == pytest.approx(0.29)


class TestSelectInputsBySide:
    def test_selects_raw_inputs_by_side(self) -> None:
        is_ce = np.array([True, False])
        entry_ce = np.array([100.0, 200.0])
        path_ce = np.array([[101.0, 102.0], [201.0, 202.0]])
        lens_ce = np.array([2, 2])
        cost_ce = np.array([0.01, 0.02])
        entry_pe = np.array([90.0, 80.0])
        path_pe = np.array([[91.0, 92.0], [81.0, 82.0]])
        lens_pe = np.array([2, 2])
        cost_pe = np.array([0.03, 0.04])

        entry, path, lens, cost = select_inputs_by_side(
            is_ce, entry_ce, path_ce, lens_ce, cost_ce, entry_pe, path_pe, lens_pe, cost_pe,
        )
        assert entry.tolist() == [100.0, 80.0]
        assert path.tolist() == [[101.0, 102.0], [81.0, 82.0]]
        assert lens.tolist() == [2, 2]
        assert cost.tolist() == [0.01, 0.04]


class TestSummarizeReturns:
    def test_computes_win_loss_stats(self) -> None:
        arr = np.array([0.10, 0.20, -0.05, -0.10, 0.0])
        stats = summarize_returns(arr)
        assert stats["n"] == 5
        assert stats["win_rate"] == pytest.approx(2 / 5)
        assert stats["mean_win"] == pytest.approx(0.15)
        assert stats["mean_loss"] == pytest.approx((-0.05 - 0.10 + 0.0) / 3)
        assert stats["expectancy"] == pytest.approx(arr.mean())

    def test_zero_is_treated_as_a_loss_not_a_win(self) -> None:
        stats = summarize_returns(np.array([0.0, 0.0]))
        assert stats["win_rate"] == 0.0
        assert stats["mean_loss"] == pytest.approx(0.0)
        assert stats["mean_win"] is None

    def test_all_nan_returns_empty_summary(self) -> None:
        stats = summarize_returns(np.array([np.nan, np.nan]))
        assert stats["n"] == 0
        assert stats["win_rate"] is None

    def test_nan_entries_are_dropped_not_imputed(self) -> None:
        stats = summarize_returns(np.array([0.10, np.nan, -0.10]))
        assert stats["n"] == 2


class TestHardStopWhipsawReport:
    def test_whipsaw_rate_reflects_recovery_after_hypothetical_no_stop(self) -> None:
        # Entry A: stopped at stop=0.10, but the (unstopped) path recovers to
        # a net-positive close by the time cap -> whipsaw.
        a = [95.0, 88.0, 95.0, 105.0, 115.0, 120.0]
        # Entry B: stopped at stop=0.10, unstopped path stays net-negative -> not whipsaw.
        b = [95.0, 88.0, 85.0, 80.0, 75.0, 70.0]
        # Entry C: never stopped at stop=0.10 (max drawdown -3%).
        c = [98.0, 97.0, 99.0, 101.0, 103.0, 105.0]
        entry_premiums = np.array([100.0, 100.0, 100.0])
        cost_pcts = np.array([0.01, 0.01, 0.01])
        forward_paths = [ForwardPath(premiums=np.asarray(p), n_real=len(p)) for p in (a, b, c)]
        padded, lens = pad_premium_paths(forward_paths, 6)

        report = hard_stop_whipsaw_report(entry_premiums, padded, lens, cost_pcts, [0.10, 0.50])

        row_10 = next(r for r in report if r["stop_loss_pct"] == 0.10)
        assert row_10["n_stopped"] == 2
        assert row_10["whipsaw_rate"] == pytest.approx(0.5)

        row_50 = next(r for r in report if r["stop_loss_pct"] == 0.50)
        assert row_50["n_stopped"] == 0
        assert row_50["whipsaw_rate"] is None
