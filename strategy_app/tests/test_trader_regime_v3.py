from __future__ import annotations

from strategy_app.contracts import Direction
from strategy_app.engines.options_state import OptionsStateBuilder
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.trader_regime_v3 import TraderRegimeClassifierV3, TraderRegimeV3Label


def _payload(*, minutes: int, expiry: bool = False) -> dict:
    return {
        "snapshot_id": f"snap-{minutes}",
        "session_context": {
            "snapshot_id": f"snap-{minutes}",
            "timestamp": "2026-04-01T10:00:00+05:30",
            "date": "2026-04-01",
            "session_phase": "ACTIVE",
            "minutes_since_open": minutes,
            "days_to_expiry": 0 if expiry else 2,
            "is_expiry_day": expiry,
        },
        "chain_aggregates": {"atm_strike": 50000, "max_pain": 50000},
        "futures_bar": {"fut_close": 50030.0},
        "futures_derived": {
            "fut_return_5m": 0.0006,
            "fut_return_15m": 0.0012,
            "fut_return_30m": 0.0020,
            "realized_vol_30m": 0.012,
            "vol_ratio": 1.4,
            "vwap": 49980.0,
            "price_vs_vwap": 0.0010,
        },
        "opening_range": {"orh": 50020.0, "orl": 49940.0, "orh_broken": True, "orl_broken": False},
        "atm_options": {
            "atm_ce_close": 120.0,
            "atm_pe_close": 110.0,
            "atm_ce_vol_ratio": 1.6,
            "atm_pe_vol_ratio": 1.5,
            "atm_ce_iv": 0.18,
            "atm_pe_iv": 0.19,
        },
        "iv_derived": {"iv_percentile": 58.0, "iv_skew": -0.02},
        "vix_context": {"vix_spike_flag": False},
        "strikes": [
            {"strike": 49900, "ce_ltp": 145.0, "ce_oi": 23000.0, "ce_volume": 19000.0, "pe_ltp": 84.0, "pe_oi": 13000.0, "pe_volume": 12000.0},
            {"strike": 50000, "ce_ltp": 120.0, "ce_oi": 24000.0, "ce_volume": 20000.0, "pe_ltp": 115.0, "pe_oi": 20000.0, "pe_volume": 19500.0},
            {"strike": 50100, "ce_ltp": 94.0, "ce_oi": 25000.0, "ce_volume": 21000.0, "pe_ltp": 142.0, "pe_oi": 21000.0, "pe_volume": 20500.0},
        ],
    }


def test_trader_regime_v3_marks_trend_up() -> None:
    snap = SnapshotAccessor(_payload(minutes=45))
    options_state = OptionsStateBuilder().build(snap)
    regime = TraderRegimeClassifierV3().assess(snap, options_state)

    assert regime.label == TraderRegimeV3Label.TREND_UP
    assert regime.bias == Direction.CE


def test_trader_regime_v3_marks_range() -> None:
    payload = _payload(minutes=75)
    payload["futures_derived"]["fut_return_15m"] = 0.0002
    payload["futures_derived"]["fut_return_30m"] = 0.0003
    payload["futures_derived"]["price_vs_vwap"] = 0.0002
    payload["opening_range"]["orh_broken"] = False
    snap = SnapshotAccessor(payload)
    regime = TraderRegimeClassifierV3().assess(snap, OptionsStateBuilder().build(snap))

    assert regime.label == TraderRegimeV3Label.RANGE


def test_trader_regime_v3_marks_vol_expansion() -> None:
    payload = _payload(minutes=60)
    payload["futures_derived"]["realized_vol_30m"] = 0.020
    payload["futures_derived"]["vol_ratio"] = 1.7
    snap = SnapshotAccessor(payload)
    regime = TraderRegimeClassifierV3().assess(snap, OptionsStateBuilder().build(snap))

    assert regime.label == TraderRegimeV3Label.VOL_EXPANSION


def test_trader_regime_v3_marks_vol_crush() -> None:
    payload = _payload(minutes=120)
    payload["iv_derived"]["iv_percentile"] = 82.0
    payload["futures_derived"]["realized_vol_30m"] = 0.006
    payload["futures_derived"]["vol_ratio"] = 0.9
    payload["futures_derived"]["price_vs_vwap"] = 0.0002
    snap = SnapshotAccessor(payload)
    regime = TraderRegimeClassifierV3().assess(snap, OptionsStateBuilder().build(snap))

    assert regime.label == TraderRegimeV3Label.VOL_CRUSH


def test_trader_regime_v3_marks_expiry_momentum() -> None:
    payload = _payload(minutes=45, expiry=True)
    payload["futures_derived"]["vol_ratio"] = 1.6
    snap = SnapshotAccessor(payload)
    regime = TraderRegimeClassifierV3().assess(snap, OptionsStateBuilder().build(snap))

    assert regime.label == TraderRegimeV3Label.EXPIRY_MOMENTUM


def test_trader_regime_v3_marks_expiry_pinning() -> None:
    payload = _payload(minutes=180, expiry=True)
    payload["futures_bar"]["fut_close"] = 50002.0
    payload["futures_derived"]["fut_return_15m"] = 0.0002
    payload["futures_derived"]["vol_ratio"] = 1.0
    payload["futures_derived"]["price_vs_vwap"] = 0.0001
    snap = SnapshotAccessor(payload)
    regime = TraderRegimeClassifierV3().assess(snap, OptionsStateBuilder().build(snap))

    assert regime.label == TraderRegimeV3Label.EXPIRY_PINNING


def test_trader_regime_v3_marks_midday_no_trade() -> None:
    payload = _payload(minutes=180)
    payload["futures_derived"]["fut_return_15m"] = 0.0002
    payload["futures_derived"]["vol_ratio"] = 0.9
    payload["futures_derived"]["price_vs_vwap"] = 0.0001
    snap = SnapshotAccessor(payload)
    regime = TraderRegimeClassifierV3().assess(snap, OptionsStateBuilder().build(snap))

    assert regime.label == TraderRegimeV3Label.NO_TRADE
