"""Tests for ml_pipeline_2.scripts.check_option_premium_profile (BankNifty
research, 2026-09-27). Fixture convention matches
test_variable_hold_exit.py's `_snap`/`_strike_row`/day-builder helpers."""
from __future__ import annotations

import pandas as pd
import pytest

from ml_pipeline_2.scripts.check_option_premium_profile import (
    profile_one_day,
    sample_trade_dates,
)
from strategy_app.market.snapshot_accessor import SnapshotAccessor

LOT_SIZE = 30  # BankNifty


def _snap(*, timestamp: str, trade_date: str, atm_strike: int, atm_ce_close: float,
          atm_pe_close: float, strikes: list[dict]) -> SnapshotAccessor:
    payload = {
        "snapshot_id": "s1",
        "timestamp": timestamp,
        "trade_date": trade_date,
        "session_context": {"date": trade_date, "time": timestamp},
        "chain_aggregates": {"atm_strike": atm_strike},
        "atm_options": {
            "atm_ce_close": atm_ce_close, "atm_pe_close": atm_pe_close,
            "atm_ce_oi": 50000.0, "atm_pe_oi": 50000.0,
        },
        "strikes": strikes,
    }
    return SnapshotAccessor(payload)


def _strike_row(strike: int, *, ce_ltp, pe_ltp) -> dict:
    return {"strike": strike, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp}


def _day_snaps(ce_path: list[float], pe_path: list[float], *, strike: int = 52000) -> list[SnapshotAccessor]:
    """One bar per entry, 1-minute cadence starting 09:45 IST, entry premium
    fixed at the FIRST path value (so `path_extremes` on the forward path
    starting bar+1 has a well-known expected answer)."""
    start = pd.Timestamp("2026-01-05 09:45")
    out = []
    for i, (ce, pe) in enumerate(zip(ce_path, pe_path)):
        ts_ist = start + pd.Timedelta(minutes=i)
        ts_utc = ts_ist - pd.Timedelta(hours=5, minutes=30)
        out.append(
            _snap(
                timestamp=ts_utc.isoformat() + "+00:00", trade_date="2026-01-05",
                atm_strike=strike, atm_ce_close=ce_path[0], atm_pe_close=pe_path[0],
                strikes=[_strike_row(strike, ce_ltp=ce, pe_ltp=pe)],
            )
        )
    return out


class TestSampleTradeDates:
    def test_stride_1_keeps_all(self):
        dates = ["2024-11-04", "2024-11-05", "2024-11-06"]
        assert sample_trade_dates(dates, day_stride=1) == dates

    def test_stride_0_or_negative_keeps_all(self):
        dates = ["a", "b", "c"]
        assert sample_trade_dates(dates, day_stride=0) == dates

    def test_stride_5_spreads_across_full_range(self):
        dates = [f"d{i:03d}" for i in range(21)]
        out = sample_trade_dates(dates, day_stride=5)
        assert out == ["d000", "d005", "d010", "d015", "d020"]
        # spans the full range, not just a recent slice
        assert out[0] == dates[0]
        assert out[-1] != dates[-1] or len(dates) % 5 == 1


class TestProfileOneDay:
    def test_ce_and_pe_extremes_match_known_path(self):
        # entry premium = 100 (both legs); CE rallies to +30% then pulls back
        # to +10%, PE only ever falls (to -20%).
        ce_path = [100, 110, 120, 130, 110]
        pe_path = [100, 95, 90, 80, 85]
        snaps = _day_snaps(ce_path, pe_path)
        result = profile_one_day(snaps, lot_size=LOT_SIZE, bar_stride=100, max_bars=90, session_end_min=15 * 60 + 5)
        # bar_stride=100 with 5 bars -> only entry index 0 is sampled
        assert len(result["ce_extremes"]) == 1
        assert len(result["pe_extremes"]) == 1
        ce_ex = result["ce_extremes"][0]
        pe_ex = result["pe_extremes"][0]
        assert ce_ex["max_gain_pct"] == pytest.approx(0.30)
        assert pe_ex["max_drawdown_pct"] == pytest.approx(-0.20)
        assert result["ce_entry_premiums"] == [100.0]
        assert result["pe_entry_premiums"] == [100.0]

    def test_bar_stride_controls_sample_count(self):
        ce_path = [100.0] * 10
        pe_path = [100.0] * 10
        snaps = _day_snaps(ce_path, pe_path)
        # stride=1 samples every bar with a forward bar available (bars 0..8, since bar 9 has none)
        result = profile_one_day(snaps, lot_size=LOT_SIZE, bar_stride=1, max_bars=90, session_end_min=15 * 60 + 5)
        assert len(result["ce_extremes"]) == 9

    def test_last_bar_has_no_forward_path_and_is_excluded(self):
        ce_path = [100.0, 105.0]
        pe_path = [100.0, 95.0]
        snaps = _day_snaps(ce_path, pe_path)
        result = profile_one_day(snaps, lot_size=LOT_SIZE, bar_stride=1, max_bars=90, session_end_min=15 * 60 + 5)
        # bar 0 has a forward bar (bar 1); bar 1 has none -> excluded
        assert len(result["ce_extremes"]) == 1
