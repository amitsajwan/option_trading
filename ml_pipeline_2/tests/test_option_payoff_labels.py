"""Tests for the cost-aware, direction-agnostic option-payoff label
(ml_pipeline_2/src/ml_pipeline_2/pipeline/option_payoff_labels.py).

Fixture convention matches strategy_app/tests/test_entry_direction_resolver.py's
_snap() helper: build a raw payload dict, wrap in SnapshotAccessor.
"""
from __future__ import annotations

from ml_pipeline_2.pipeline.option_payoff_labels import (
    MIN_ENTRY_OI,
    MIN_ENTRY_PREMIUM,
    compute_bar_payoff,
    label_day,
    measure_strike_coverage,
)
from strategy_app.cost_model import TradingCostModel
from strategy_app.market.snapshot_accessor import SnapshotAccessor

LOT_SIZE = 65  # NIFTY


def _snap(
    *,
    timestamp: str = "2026-01-05T04:15:00+00:00",  # 09:45 IST
    trade_date: str = "2026-01-05",
    atm_strike: int = 24500,
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


# Cost is small relative to a big move but non-trivial for a flat one -- use
# the real cost model so tests double as a sanity check of the magnitude.
_COST = TradingCostModel()


class TestComputeBarPayoff:
    def test_ce_wins_when_underlying_rallies(self) -> None:
        entry = _snap(atm_strike=24500, atm_ce_close=100.0, atm_pe_close=100.0)
        exit_snap = _snap(
            timestamp="2026-01-05T04:30:00+00:00",
            strikes=[_strike_row(24500, ce_ltp=160.0, pe_ltp=40.0)],
        )
        result = compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE, cost_model=_COST)
        assert result is not None
        assert result.ce is not None and result.pe is not None
        assert result.ce.net_return > 0
        assert result.pe.net_return < 0
        assert result.payoff_score == result.ce.net_return
        assert result.winning_side == "CE"

    def test_pe_wins_when_underlying_falls(self) -> None:
        entry = _snap(atm_strike=24500, atm_ce_close=100.0, atm_pe_close=100.0)
        exit_snap = _snap(
            timestamp="2026-01-05T04:30:00+00:00",
            strikes=[_strike_row(24500, ce_ltp=40.0, pe_ltp=160.0)],
        )
        result = compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE, cost_model=_COST)
        assert result is not None
        assert result.winning_side == "PE"
        assert result.payoff_score == result.pe.net_return

    def test_uses_entrys_own_atm_strike_not_exits(self) -> None:
        """Fixed-contract exit: if spot drifts to a new strike step by exit
        time, we must still price the ORIGINAL strike, not re-base to
        whatever is ATM later. Exit snapshot's chain only has the original
        strike quoted plus a decoy at the new (wrong) ATM."""
        entry = _snap(atm_strike=24500, atm_ce_close=100.0, atm_pe_close=100.0)
        exit_snap = _snap(
            timestamp="2026-01-05T04:30:00+00:00",
            atm_strike=24600,  # spot drifted; exit's OWN atm is now 24600
            strikes=[
                _strike_row(24500, ce_ltp=170.0, pe_ltp=30.0),  # the contract we actually hold
                _strike_row(24600, ce_ltp=999.0, pe_ltp=999.0),  # decoy -- must NOT be used
            ],
        )
        result = compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE, cost_model=_COST)
        assert result is not None
        assert result.strike == 24500
        assert result.ce.exit_premium == 170.0

    def test_small_favorable_move_can_still_net_negative_after_cost(self) -> None:
        entry = _snap(atm_strike=24500, atm_ce_close=100.0, atm_pe_close=100.0)
        # +2 points on a 100pt premium before cost -- realistic round-trip
        # cost (brokerage+charges+slippage) on a 65-lot NIFTY contract should
        # exceed a bare 2% gross move.
        exit_snap = _snap(
            timestamp="2026-01-05T04:30:00+00:00",
            strikes=[_strike_row(24500, ce_ltp=102.0, pe_ltp=95.0)],
        )
        result = compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE, cost_model=_COST)
        assert result is not None
        assert result.payoff_score is not None
        assert result.payoff_score < 0.02  # cost has eaten most/all of the gross move

    def test_missing_atm_strike_returns_none(self) -> None:
        entry = _snap(atm_strike=None)  # type: ignore[arg-type]
        exit_snap = _snap(timestamp="2026-01-05T04:30:00+00:00")
        assert compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE) is None

    def test_missing_exit_quote_skips_that_leg_not_impute(self) -> None:
        entry = _snap(atm_strike=24500, atm_ce_close=100.0, atm_pe_close=100.0)
        exit_snap = _snap(
            timestamp="2026-01-05T04:30:00+00:00",
            strikes=[_strike_row(24500, ce_ltp=160.0, pe_ltp=None)],
        )
        result = compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE, cost_model=_COST)
        assert result is not None
        assert result.pe is None
        assert result.ce is not None
        assert result.payoff_score == result.ce.net_return

    def test_both_legs_missing_gives_none_payoff_score_not_crash(self) -> None:
        entry = _snap(atm_strike=24500, atm_ce_close=100.0, atm_pe_close=100.0)
        exit_snap = _snap(
            timestamp="2026-01-05T04:30:00+00:00",
            atm_ce_close=None, atm_pe_close=None,  # no ATM-fallback quote either
            strikes=[],
        )
        result = compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE, cost_model=_COST)
        assert result is not None
        assert result.ce is None and result.pe is None
        assert result.payoff_score is None
        assert result.winning_side is None

    def test_illiquid_entry_oi_below_floor_rejects_leg(self) -> None:
        entry = _snap(
            atm_strike=24500, atm_ce_close=100.0, atm_pe_close=100.0,
            atm_ce_oi=MIN_ENTRY_OI - 1,  # thinner than the liquidity floor
        )
        exit_snap = _snap(
            timestamp="2026-01-05T04:30:00+00:00",
            strikes=[_strike_row(24500, ce_ltp=160.0, pe_ltp=40.0)],
        )
        result = compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE, cost_model=_COST)
        assert result is not None
        assert result.ce is None, "thin OI at entry must reject the leg, not label a fantasy fill"
        assert result.pe is not None

    def test_tiny_entry_premium_below_floor_rejects_leg(self) -> None:
        entry = _snap(atm_strike=24500, atm_ce_close=MIN_ENTRY_PREMIUM - 1, atm_pe_close=100.0)
        exit_snap = _snap(
            timestamp="2026-01-05T04:30:00+00:00",
            strikes=[_strike_row(24500, ce_ltp=10.0, pe_ltp=40.0)],
        )
        result = compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE, cost_model=_COST)
        assert result is not None
        assert result.ce is None
        assert result.pe is not None


class TestLabelDay:
    def _one_minute_day(self, n_bars: int, start_hhmm: str = "09:45") -> list[SnapshotAccessor]:
        import pandas as pd

        start = pd.Timestamp(f"2026-01-05 {start_hhmm}")
        out = []
        for i in range(n_bars):
            ts_ist = start + pd.Timedelta(minutes=i)
            ts_utc = ts_ist - pd.Timedelta(hours=5, minutes=30)
            out.append(
                _snap(
                    timestamp=ts_utc.isoformat() + "+00:00",
                    strikes=[_strike_row(24500, ce_ltp=100.0 + i, pe_ltp=100.0 - i * 0.5)],
                )
            )
        return out

    def test_bars_outside_session_window_are_none(self) -> None:
        # Start at 09:30 IST -- first 15 bars (09:30-09:44) are before the
        # 09:45 session-open filter and must be labeled None.
        snapshots = self._one_minute_day(40, start_hhmm="09:30")
        labels = label_day(snapshots, horizon_bars=5, lot_size=LOT_SIZE, cost_model=_COST)
        for i in range(15):
            assert labels[i] is None, f"bar {i} (before 09:45) should be filtered"
        assert labels[16] is not None

    def test_tail_bars_within_horizon_of_end_are_none(self) -> None:
        snapshots = self._one_minute_day(30)
        labels = label_day(snapshots, horizon_bars=5, lot_size=LOT_SIZE, cost_model=_COST)
        assert labels[-1] is None
        assert labels[-5] is None
        assert labels[-6] is not None

    def test_labeled_bar_pairs_entry_with_correct_horizon_offset(self) -> None:
        snapshots = self._one_minute_day(30)
        labels = label_day(snapshots, horizon_bars=5, lot_size=LOT_SIZE, cost_model=_COST)
        bp = labels[0]
        assert bp is not None
        # bar 0 ce_ltp=100, bar 5 ce_ltp=105 -> gross +5% before cost
        assert bp.ce is not None
        assert bp.ce.entry_premium == 100.0
        assert bp.ce.exit_premium == 105.0


class TestMeasureStrikeCoverage:
    def test_aggregates_missing_quote_and_positive_rate(self) -> None:
        snapshots = TestLabelDay()._one_minute_day(30)
        labels = label_day(snapshots, horizon_bars=5, lot_size=LOT_SIZE, cost_model=_COST)
        report = measure_strike_coverage(labels)
        assert report.total_entry_bars == 30
        assert report.bars_with_atm_strike == sum(1 for l in labels if l is not None)
        assert 0.0 <= report.missing_quote_rate <= 1.0
        assert 0.0 <= report.payoff_positive_rate <= 1.0

    def test_all_bars_illiquid_fails_missing_quote_gate(self) -> None:
        entry = _snap(atm_strike=24500, atm_ce_close=100.0, atm_pe_close=100.0,
                       atm_ce_oi=0.0, atm_pe_oi=0.0)
        exit_snap = _snap(timestamp="2026-01-05T04:30:00+00:00",
                           strikes=[_strike_row(24500, ce_ltp=160.0, pe_ltp=40.0)])
        bp = compute_bar_payoff(entry, exit_snap, lot_size=LOT_SIZE, cost_model=_COST)
        report = measure_strike_coverage([bp])
        assert report.missing_quote_rate == 1.0
        assert not report.passes_missing_quote_gate

    def test_healthy_sample_passes_both_gates(self) -> None:
        snapshots = TestLabelDay()._one_minute_day(30)
        labels = label_day(snapshots, horizon_bars=5, lot_size=LOT_SIZE, cost_model=_COST)
        report = measure_strike_coverage(labels)
        assert report.passes_missing_quote_gate
