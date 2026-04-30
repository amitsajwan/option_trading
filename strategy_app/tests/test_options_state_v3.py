from __future__ import annotations

from strategy_app.contracts import Direction
from strategy_app.engines.options_state import OptionsStateBuilder
from strategy_app.engines.snapshot_accessor import SnapshotAccessor


def _payload(*, strikes: list[dict], atm_strike: int = 50000, fut_close: float = 50020.0, dte: int = 2) -> dict:
    return {
        "snapshot_id": "snap-options-v3",
        "session_context": {
            "snapshot_id": "snap-options-v3",
            "timestamp": "2026-04-01T10:00:00+05:30",
            "date": "2026-04-01",
            "session_phase": "ACTIVE",
            "minutes_since_open": 45,
            "days_to_expiry": dte,
            "is_expiry_day": dte == 0,
        },
        "chain_aggregates": {"atm_strike": atm_strike},
        "futures_bar": {"fut_close": fut_close},
        "futures_derived": {"realized_vol_30m": 0.012},
        "iv_derived": {"iv_percentile": 58.0, "iv_skew": -0.02},
        "atm_options": {
            "atm_ce_close": 120.0,
            "atm_pe_close": 115.0,
            "atm_ce_vol_ratio": 1.6,
            "atm_pe_vol_ratio": 1.5,
            "atm_ce_iv": 0.18,
            "atm_pe_iv": 0.19,
        },
        "strikes": strikes,
    }


def test_options_state_builder_handles_full_chain() -> None:
    builder = OptionsStateBuilder()
    snap = SnapshotAccessor(
        _payload(
            strikes=[
                {"strike": 49800, "ce_ltp": 170.0, "ce_oi": 22000.0, "ce_volume": 18000.0, "ce_delta": 0.72, "pe_ltp": 72.0, "pe_oi": 12000.0, "pe_volume": 11000.0, "pe_delta": -0.28},
                {"strike": 49900, "ce_ltp": 145.0, "ce_oi": 23000.0, "ce_volume": 19000.0, "ce_delta": 0.61, "pe_ltp": 84.0, "pe_oi": 13000.0, "pe_volume": 12000.0, "pe_delta": -0.39},
                {"strike": 50000, "ce_ltp": 120.0, "ce_oi": 24000.0, "ce_volume": 20000.0, "ce_delta": 0.51, "pe_ltp": 115.0, "pe_oi": 20000.0, "pe_volume": 19500.0, "pe_delta": -0.49},
                {"strike": 50100, "ce_ltp": 94.0, "ce_oi": 25000.0, "ce_volume": 21000.0, "ce_delta": 0.41, "pe_ltp": 142.0, "pe_oi": 21000.0, "pe_volume": 20500.0, "pe_delta": -0.59},
                {"strike": 50200, "ce_ltp": 76.0, "ce_oi": 26000.0, "ce_volume": 21500.0, "ce_delta": 0.31, "pe_ltp": 168.0, "pe_oi": 22000.0, "pe_volume": 21200.0, "pe_delta": -0.69},
                {"strike": 50300, "ce_ltp": 61.0, "ce_oi": 27000.0, "ce_volume": 22000.0, "ce_delta": 0.23, "pe_ltp": 191.0, "pe_oi": 23000.0, "pe_volume": 21800.0, "pe_delta": -0.77},
                {"strike": 50400, "ce_ltp": 48.0, "ce_oi": 27500.0, "ce_volume": 22500.0, "ce_delta": 0.17, "pe_ltp": 214.0, "pe_oi": 23500.0, "pe_volume": 22000.0, "pe_delta": -0.83},
            ]
        )
    )

    state = builder.build(snap)

    assert state.chain_quality == "full"
    assert state.strike_step == 100
    assert state.side_candidates(Direction.CE)[2].delta == 0.51


def test_options_state_builder_marks_sparse_chain() -> None:
    state = OptionsStateBuilder().build(
        SnapshotAccessor(
            _payload(
                strikes=[
                    {"strike": 49900, "ce_ltp": 145.0, "ce_oi": 23000.0, "ce_volume": 19000.0, "pe_ltp": 84.0, "pe_oi": 13000.0, "pe_volume": 12000.0},
                    {"strike": 50000, "ce_ltp": 120.0, "ce_oi": 24000.0, "ce_volume": 20000.0, "pe_ltp": 115.0, "pe_oi": 20000.0, "pe_volume": 19500.0},
                    {"strike": 50100, "ce_ltp": 94.0, "ce_oi": 25000.0, "ce_volume": 21000.0, "pe_ltp": 142.0, "pe_oi": 21000.0, "pe_volume": 20500.0},
                ]
            )
        )
    )

    assert state.chain_quality == "sparse"
    assert state.strike_count == 3


def test_options_state_builder_estimates_missing_greeks() -> None:
    state = OptionsStateBuilder().build(
        SnapshotAccessor(
            _payload(
                strikes=[
                    {"strike": 49900, "ce_ltp": 145.0, "ce_iv": 0.17, "ce_oi": 23000.0, "ce_volume": 19000.0, "pe_ltp": 84.0, "pe_iv": 0.20, "pe_oi": 13000.0, "pe_volume": 12000.0},
                    {"strike": 50000, "ce_ltp": 120.0, "ce_iv": 0.18, "ce_oi": 24000.0, "ce_volume": 20000.0, "pe_ltp": 115.0, "pe_iv": 0.19, "pe_oi": 20000.0, "pe_volume": 19500.0},
                    {"strike": 50100, "ce_ltp": 94.0, "ce_iv": 0.19, "ce_oi": 25000.0, "ce_volume": 21000.0, "pe_ltp": 142.0, "pe_iv": 0.18, "pe_oi": 21000.0, "pe_volume": 20500.0},
                ]
            )
        )
    )

    ce_atm = state.side_candidates(Direction.CE)[1]
    pe_atm = state.side_candidates(Direction.PE)[1]

    assert ce_atm.delta is not None
    assert pe_atm.delta is not None
    assert ce_atm.delta_source == "estimated"
    assert pe_atm.delta_source == "estimated"
