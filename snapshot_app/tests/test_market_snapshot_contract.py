from __future__ import annotations

from snapshot_app.market_snapshot_contract import validate_market_snapshot
from strategy_app.engines.snapshot_accessor import SnapshotAccessor


def _valid_snapshot() -> dict:
    return {
        "schema_name": "MarketSnapshot",
        "schema_version": "3.0",
        "snapshot_id": "20260317_0915",
        "instrument": "BANKNIFTY26MARFUT",
        "trade_date": "2026-03-17",
        "timestamp": "2026-03-17T09:15:00+05:30",
        "session_context": {
            "snapshot_id": "20260317_0915",
            "timestamp": "2026-03-17T09:15:00+05:30",
            "date": "2026-03-17",
            "time": "09:15:00",
            "minutes_since_open": 0,
            "day_of_week": 1,
            "days_to_expiry": 2,
            "is_expiry_day": False,
            "session_phase": "DISCOVERY",
        },
        "futures_bar": {
            "fut_open": 50000.0,
            "fut_high": 50010.0,
            "fut_low": 49990.0,
            "fut_close": 50005.0,
            "fut_volume": 1000,
            "fut_oi": 2000,
        },
        "futures_derived": {
            "fut_return_5m": 0.0,
            "fut_return_15m": 0.0,
            "fut_return_30m": 0.0,
            "realized_vol_30m": 0.12,
            "vol_ratio": 1.1,
            "fut_volume_ratio": 1.0,
            "fut_oi_change_30m": 0,
            "ema_9": 50001.0,
            "ema_21": 50000.0,
            "ema_50": 49995.0,
            "vwap": 50002.0,
            "price_vs_vwap": 0.0,
        },
        "mtf_derived": {},
        "opening_range": {
            "orh": 50020.0,
            "orl": 49980.0,
            "or_width": 40.0,
            "price_vs_orh": -0.0003,
            "price_vs_orl": 0.0005,
            "orh_broken": False,
            "orl_broken": False,
        },
        "vix_context": {
            "vix_current": 15.0,
            "vix_prev_close": 14.8,
            "vix_intraday_chg": 1.35,
            "vix_regime": "NORMAL",
            "vix_spike_flag": False,
        },
        "strikes": [
            {
                "strike": 50000,
                "ce_ltp": 100.0,
                "pe_ltp": 90.0,
                "ce_oi": 10000,
                "pe_oi": 11000,
                "ce_volume": 1000,
                "pe_volume": 900,
                "ce_iv": 0.18,
                "pe_iv": 0.19,
                "ce_open": 98.0,
                "ce_high": 101.0,
                "ce_low": 97.0,
                "pe_open": 89.0,
                "pe_high": 92.0,
                "pe_low": 88.0,
            }
        ],
        "chain_aggregates": {
            "atm_strike": 50000,
            "strike_count": 1,
            "total_ce_oi": 10000,
            "total_pe_oi": 11000,
            "pcr": 1.1,
            "pcr_change_30m": 0.01,
            "max_pain": 50000,
            "ce_oi_top_strike": 50000,
            "pe_oi_top_strike": 50000,
        },
        "atm_options": {
            "atm_ce_strike": 50000,
            "atm_ce_open": 98.0,
            "atm_ce_high": 101.0,
            "atm_ce_low": 97.0,
            "atm_ce_close": 100.0,
            "atm_ce_volume": 1000,
            "atm_ce_oi": 10000,
            "atm_ce_oi_change_30m": 250,
            "atm_ce_iv": 0.18,
            "atm_ce_vol_ratio": 1.1,
            "atm_pe_strike": 50000,
            "atm_pe_open": 89.0,
            "atm_pe_high": 92.0,
            "atm_pe_low": 88.0,
            "atm_pe_close": 90.0,
            "atm_pe_volume": 900,
            "atm_pe_oi": 11000,
            "atm_pe_oi_change_30m": 200,
            "atm_pe_iv": 0.19,
            "atm_pe_vol_ratio": 1.0,
        },
        "iv_derived": {
            "iv_skew": -0.01,
            "iv_skew_dir": "PUT_FEAR",
            "iv_percentile": 62.0,
            "iv_regime": "NEUTRAL",
            "iv_expiry_type": "NON_EXPIRY",
        },
        "session_levels": {
            "prev_day_high": 50100.0,
            "prev_day_low": 49800.0,
            "prev_day_close": 49990.0,
            "week_high": 50300.0,
            "week_low": 49600.0,
            "overnight_gap": 0.0002,
            "prev_day_pcr": 1.04,
            "prev_day_max_pain": 49900,
        },
    }


def test_market_snapshot_contract_accepts_valid_snapshot() -> None:
    report = validate_market_snapshot(_valid_snapshot(), raise_on_error=False)
    assert report["ok"] is True
    assert report["error_count"] == 0


def test_snapshot_accessor_reads_final_market_snapshot_contract() -> None:
    snap = SnapshotAccessor(_valid_snapshot())
    assert snap.snapshot_id == "20260317_0915"
    assert snap.trade_date == "2026-03-17"
    assert snap.atm_ce_close == 100.0
    assert snap.pcr == 1.1
