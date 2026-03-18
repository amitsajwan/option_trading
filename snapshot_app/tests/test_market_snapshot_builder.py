from __future__ import annotations

import numpy as np
import pandas as pd

from snapshot_app.core.market_snapshot import (
    MarketSnapshotState,
    _normalize_ohlc_frame,
    build_market_snapshot,
    prepare_market_snapshot_window,
)
from snapshot_app.core.market_snapshot_contract import validate_market_snapshot
from snapshot_app.core.stage_views import (
    project_stage1_entry_view,
    project_stage2_direction_view,
    project_stage3_recipe_view,
)


def _ohlc_frame(*, start: str, periods: int, closes: list[float]) -> pd.DataFrame:
    ts = pd.date_range(start=start, periods=periods, freq="min")
    closes_arr = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes_arr[0] - 5.0], closes_arr[:-1]])
    highs = np.maximum(opens, closes_arr) + 8.0
    lows = np.minimum(opens, closes_arr) - 8.0
    volume = np.linspace(1_000.0, 1_800.0, periods)
    oi = np.linspace(20_000.0, 21_500.0, periods)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes_arr,
            "volume": volume,
            "oi": oi,
        }
    )


def _chain_for_price(
    *,
    fut_close: float,
    atm_override: int | None = None,
    ce_bump: float = 0.0,
    pe_bump: float = 0.0,
    ce_oi_bump: float = 0.0,
    pe_oi_bump: float = 0.0,
) -> dict[str, object]:
    strikes: list[dict[str, float | int]] = []
    atm_strike = int(atm_override or round(fut_close / 100.0) * 100)
    for strike in (atm_strike - 300, atm_strike - 200, atm_strike - 100, atm_strike, atm_strike + 100, atm_strike + 200, atm_strike + 300):
        distance = abs(strike - fut_close)
        ce_ltp = max(8.0, 125.0 - (distance / 8.0))
        pe_ltp = max(8.0, 118.0 - (distance / 8.0))
        if strike == atm_strike:
            ce_ltp += ce_bump
            pe_ltp += pe_bump
        ce_oi = max(500.0, 14_000.0 - (abs(strike - atm_strike) * 10.0))
        pe_oi = max(500.0, 13_500.0 - (abs(strike - atm_strike) * 10.0))
        ce_volume = max(100.0, 1_600.0 - (abs(strike - atm_strike) * 1.5))
        pe_volume = max(100.0, 1_500.0 - (abs(strike - atm_strike) * 1.5))
        if strike == atm_strike:
            ce_oi += ce_oi_bump
            pe_oi += pe_oi_bump
        strikes.append(
            {
                "strike": strike,
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_volume": ce_volume,
                "pe_volume": pe_volume,
                "ce_iv": 0.18 + ((strike - atm_strike) / 10000.0),
                "pe_iv": 0.19 - ((strike - atm_strike) / 10000.0),
                "ce_open": ce_ltp - 2.0,
                "ce_high": ce_ltp + 3.0,
                "ce_low": ce_ltp - 4.0,
                "pe_open": pe_ltp - 2.0,
                "pe_high": pe_ltp + 3.0,
                "pe_low": pe_ltp - 4.0,
            }
        )
    return {
        "expiry": "2026-03-26",
        "pcr": None,
        "max_pain": atm_strike,
        "strikes": strikes,
    }


def test_build_market_snapshot_populates_final_contract_and_stage_views() -> None:
    state = MarketSnapshotState()
    closes = [50_000.0 + (idx * 0.6) for idx in range(45)]
    bars = _ohlc_frame(start="2026-03-17 09:15:00", periods=len(closes), closes=closes)

    snapshot_1 = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=bars.iloc[:44],
        chain=_chain_for_price(fut_close=float(bars.iloc[43]["close"]), atm_override=50000),
        state=state,
    )
    snapshot_2 = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=bars.iloc[:45],
        chain=_chain_for_price(
            fut_close=float(bars.iloc[44]["close"]),
            atm_override=50000,
            ce_bump=4.0,
            pe_bump=-3.0,
            ce_oi_bump=120.0,
            pe_oi_bump=-90.0,
        ),
        state=state,
    )

    report = validate_market_snapshot(snapshot_2, raise_on_error=False)
    assert report["ok"] is True
    assert snapshot_2["session_context"]["minutes_to_close"] == 331
    assert snapshot_2["chain_aggregates"]["atm_straddle_price"] is not None
    assert snapshot_2["ladder_aggregates"]["near_atm_pcr"] is not None
    assert snapshot_2["atm_options"]["atm_ce_return_1m"] is not None
    assert snapshot_2["atm_options"]["atm_pe_oi_change_1m"] is not None

    stage1 = project_stage1_entry_view(snapshot_2)
    stage2 = project_stage2_direction_view(snapshot_2)
    stage3 = project_stage3_recipe_view(snapshot_2)

    assert stage1["near_atm_pcr"] == snapshot_2["ladder_aggregates"]["near_atm_pcr"]
    assert stage2["atm_ce_pe_price_diff"] == snapshot_2["atm_options"]["atm_ce_pe_price_diff"]
    assert stage3["atm_straddle_pct"] == snapshot_2["chain_aggregates"]["atm_straddle_pct"]
    assert snapshot_1["chain_aggregates"]["atm_strike"] == 50000
    assert snapshot_2["chain_aggregates"]["atm_strike"] == 50000


def test_build_market_snapshot_does_not_cross_compare_changed_atm_strike() -> None:
    state = MarketSnapshotState()
    closes = [50_020.0 + (idx * 0.5) for idx in range(39)] + [50_120.0]
    bars = _ohlc_frame(start="2026-03-17 09:15:00", periods=len(closes), closes=closes)

    first_snapshot = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=bars.iloc[:39],
        chain=_chain_for_price(fut_close=float(bars.iloc[38]["close"]), atm_override=50000),
        state=state,
    )
    second_snapshot = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=bars.iloc[:40],
        chain=_chain_for_price(
            fut_close=float(bars.iloc[39]["close"]),
            atm_override=50100,
            ce_bump=6.0,
            pe_bump=5.0,
            ce_oi_bump=300.0,
            pe_oi_bump=250.0,
        ),
        state=state,
    )

    assert first_snapshot["chain_aggregates"]["atm_strike"] == 50000
    assert second_snapshot["chain_aggregates"]["atm_strike"] == 50100
    assert second_snapshot["atm_options"]["atm_ce_return_1m"] is None
    assert second_snapshot["atm_options"]["atm_pe_return_1m"] is None
    assert second_snapshot["atm_options"]["atm_ce_oi_change_1m"] is None
    assert second_snapshot["atm_options"]["atm_pe_oi_change_1m"] is None


def test_normalize_ohlc_frame_drops_invalid_timestamp_rows() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": ["2026-03-17T09:15:00+05:30", "not-a-timestamp"],
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.5, 1.5],
            "close": [1.2, 2.2],
            "volume": [100.0, 200.0],
            "oi": [1000.0, 2000.0],
        }
    )

    normalized = _normalize_ohlc_frame(frame)

    assert len(normalized) == 1
    assert pd.Timestamp(normalized.iloc[0]["timestamp"]).isoformat() == "2026-03-17T09:15:00"


def test_build_market_snapshot_realized_vol_30m_uses_sample_std() -> None:
    state = MarketSnapshotState()
    closes = [50_000.0 + (idx * 5.0) for idx in range(31)]
    bars = _ohlc_frame(start="2026-03-17 09:15:00", periods=len(closes), closes=closes)

    snapshot = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=bars,
        chain=_chain_for_price(fut_close=float(bars.iloc[-1]["close"]), atm_override=50100),
        state=state,
    )

    ret_1m = pd.Series(closes, dtype=float).pct_change(fill_method=None)
    expected = float(ret_1m.rolling(30, min_periods=30).std(ddof=1).iloc[-1] * np.sqrt(252.0 * 375.0))
    actual = float(snapshot["futures_derived"]["realized_vol_30m"])
    assert np.isclose(actual, expected, rtol=1e-12, atol=1e-12)


def test_build_market_snapshot_prepared_window_matches_direct_mid_session() -> None:
    day1 = _ohlc_frame(
        start="2026-03-16 09:15:00",
        periods=45,
        closes=[49_800.0 + (idx * 0.8) for idx in range(45)],
    )
    day2 = _ohlc_frame(
        start="2026-03-17 09:15:00",
        periods=45,
        closes=[50_050.0 + (idx * 1.1) for idx in range(45)],
    )
    full_bars = pd.concat([day1, day2], ignore_index=True)
    direct_index = len(day1) + 30
    prepared = prepare_market_snapshot_window(
        full_bars,
        current_trade_date=pd.Timestamp("2026-03-17"),
    )
    chain = _chain_for_price(
        fut_close=float(full_bars.iloc[direct_index]["close"]),
        atm_override=50100,
        ce_bump=3.0,
        pe_bump=-2.0,
    )

    direct_snapshot = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=full_bars.iloc[: direct_index + 1],
        chain=chain,
        state=MarketSnapshotState(),
    )
    prepared_snapshot = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=full_bars,
        chain=chain,
        state=MarketSnapshotState(),
        prepared_window=prepared,
        current_index=direct_index,
    )

    assert prepared_snapshot == direct_snapshot


def test_build_market_snapshot_prepared_window_preserves_stateful_atm_history() -> None:
    day1 = _ohlc_frame(
        start="2026-03-16 09:15:00",
        periods=45,
        closes=[49_900.0 + (idx * 0.7) for idx in range(45)],
    )
    day2 = _ohlc_frame(
        start="2026-03-17 09:15:00",
        periods=45,
        closes=[50_020.0 + (idx * 0.9) for idx in range(45)],
    )
    full_bars = pd.concat([day1, day2], ignore_index=True)
    first_index = len(day1) + 20
    second_index = first_index + 1
    prepared = prepare_market_snapshot_window(
        full_bars,
        current_trade_date=pd.Timestamp("2026-03-17"),
    )

    direct_state = MarketSnapshotState()
    prepared_state = MarketSnapshotState()

    first_chain = _chain_for_price(
        fut_close=float(full_bars.iloc[first_index]["close"]),
        atm_override=50000,
    )
    first_direct = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=full_bars.iloc[: first_index + 1],
        chain=first_chain,
        state=direct_state,
    )
    first_prepared = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=full_bars,
        chain=first_chain,
        state=prepared_state,
        prepared_window=prepared,
        current_index=first_index,
    )

    second_chain = _chain_for_price(
        fut_close=float(full_bars.iloc[second_index]["close"]),
        atm_override=50000,
        ce_bump=4.0,
        pe_bump=-3.0,
        ce_oi_bump=125.0,
        pe_oi_bump=-95.0,
    )
    second_direct = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=full_bars.iloc[: second_index + 1],
        chain=second_chain,
        state=direct_state,
    )
    second_prepared = build_market_snapshot(
        instrument="BANKNIFTY-I",
        ohlc=full_bars,
        chain=second_chain,
        state=prepared_state,
        prepared_window=prepared,
        current_index=second_index,
    )

    assert first_prepared == first_direct
    assert second_prepared == second_direct
