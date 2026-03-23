from __future__ import annotations

import pandas as pd

from snapshot_app.core.live_ml_flat import _build_runtime_compat_payload


def test_runtime_compat_payload_uses_elevated_vix_regime_label() -> None:
    panel = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-03-17T09:30:00")],
            "trade_date": ["2026-03-17"],
            "fut_close": [50000.0],
            "fut_oi": [1000.0],
        }
    )
    payload = _build_runtime_compat_payload(
        feature_row={
            "snapshot_id": "20260317_0930",
            "ctx_dte_days": 2,
            "ctx_is_expiry_day": 0,
            "vix_prev_close": 18.0,
        },
        panel=panel,
        chain={"strikes": []},
        vix_live_current=22.0,
        ts=pd.Timestamp("2026-03-17T09:30:00"),
    )

    assert payload["vix_context"]["vix_regime"] == "ELEVATED"


def test_runtime_compat_payload_preserves_zero_atm_values() -> None:
    panel = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-03-17T09:30:00")],
            "trade_date": ["2026-03-17"],
            "fut_close": [50000.0],
            "fut_oi": [1000.0],
        }
    )
    payload = _build_runtime_compat_payload(
        feature_row={
            "snapshot_id": "20260317_0930",
            "ctx_dte_days": 2,
            "ctx_is_expiry_day": 0,
            "opt_0_ce_close": 9.0,
            "opt_0_pe_close": 11.0,
            "opt_0_ce_open": 8.0,
            "opt_0_pe_open": 10.0,
            "opt_0_ce_high": 12.0,
            "opt_0_pe_high": 13.0,
            "opt_0_ce_low": 7.0,
            "opt_0_pe_low": 9.0,
            "opt_0_ce_volume": 15.0,
            "opt_0_pe_volume": 16.0,
            "opt_0_ce_oi": 17.0,
            "opt_0_pe_oi": 18.0,
            "opt_flow_atm_strike": 50000,
        },
        panel=panel,
        chain={
            "strikes": [
                {
                    "strike": 50000,
                    "ce_ltp": 0.0,
                    "pe_ltp": 0.0,
                    "ce_open": 0.0,
                    "pe_open": 0.0,
                    "ce_high": 0.0,
                    "pe_high": 0.0,
                    "ce_low": 0.0,
                    "pe_low": 0.0,
                    "ce_volume": 0.0,
                    "pe_volume": 0.0,
                    "ce_oi": 0.0,
                    "pe_oi": 0.0,
                }
            ]
        },
        vix_live_current=None,
        ts=pd.Timestamp("2026-03-17T09:30:00"),
    )

    atm = payload["atm_options"]
    assert atm["atm_ce_close"] == 0.0
    assert atm["atm_pe_close"] == 0.0
    assert atm["atm_ce_open"] == 0.0
    assert atm["atm_pe_open"] == 0.0
    assert atm["atm_ce_high"] == 0.0
    assert atm["atm_pe_high"] == 0.0
    assert atm["atm_ce_low"] == 0.0
    assert atm["atm_pe_low"] == 0.0
    assert atm["atm_ce_volume"] == 0.0
    assert atm["atm_pe_volume"] == 0.0
    assert atm["atm_ce_oi"] == 0.0
    assert atm["atm_pe_oi"] == 0.0


def test_runtime_compat_payload_uses_feature_row_fallback_for_iv_and_fut_close() -> None:
    panel = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-03-17T09:30:00")],
            "trade_date": ["2026-03-17"],
            "fut_close": [50000.0],
            "fut_oi": [1000.0],
        }
    )
    payload = _build_runtime_compat_payload(
        feature_row={
            "snapshot_id": "20260317_0930",
            "ctx_dte_days": 2,
            "ctx_is_expiry_day": 0,
            "px_fut_close": 0.0,
            "fut_close": 50000.0,
            "opt_0_ce_iv": 0.16,
            "opt_0_pe_iv": 0.19,
            "opt_flow_atm_strike": 50000,
        },
        panel=panel,
        chain={"strikes": [{"strike": 50000, "ce_ltp": 10.0, "pe_ltp": 12.0}]},
        vix_live_current=None,
        ts=pd.Timestamp("2026-03-17T09:30:00"),
    )

    assert payload["futures_bar"]["fut_close"] == 0.0
    assert payload["atm_options"]["atm_ce_iv"] == 0.16
    assert payload["atm_options"]["atm_pe_iv"] == 0.19
