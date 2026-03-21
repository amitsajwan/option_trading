from __future__ import annotations

import pandas as pd

from snapshot_app.core.snapshot_ml_flat_contract import _check_tier2, validate_snapshot_ml_flat_rows


def test_snapshot_ml_flat_runtime_contract_accepts_minimal_valid_row() -> None:
    row = {
        "trade_date": "2026-03-16",
        "year": 2026,
        "instrument": "BANKNIFTY26MARFUT",
        "timestamp": "2026-03-16T09:30:00+05:30",
        "snapshot_id": "2026-03-16T09:30:00+05:30",
        "schema_name": "SnapshotMLFlat",
        "schema_version": "3.0",
        "build_source": "live",
        "build_run_id": "test_run",
        "px_fut_open": 50000.0,
        "px_fut_high": 50010.0,
        "px_fut_low": 49990.0,
        "px_fut_close": 50005.0,
        "px_spot_open": 49950.0,
        "px_spot_high": 49960.0,
        "px_spot_low": 49940.0,
        "px_spot_close": 49955.0,
        "ret_1m": 0.0,
        "ret_3m": 0.0,
        "ret_5m": 0.0,
        "ema_9": 50000.0,
        "ema_21": 50000.0,
        "ema_50": 50000.0,
        "ema_9_21_spread": 0.0,
        "ema_9_slope": 0.0,
        "ema_21_slope": 0.0,
        "ema_50_slope": 0.0,
        "osc_rsi_14": 50.0,
        "osc_atr_14": 100.0,
        "osc_atr_ratio": 0.002,
        "osc_atr_percentile": 0.5,
        "osc_atr_daily_percentile": 0.5,
        "vwap_fut": 50002.0,
        "vwap_distance": 0.0,
        "dist_from_day_high": -0.0001,
        "dist_from_day_low": 0.0001,
        "dist_basis": 50.0,
        "dist_basis_change_1m": 0.0,
        "fut_flow_volume": 1000.0,
        "fut_flow_oi": 2000.0,
        "fut_flow_rel_volume_20": 1.0,
        "fut_flow_volume_accel_1m": 0.0,
        "fut_flow_oi_change_1m": 0.0,
        "fut_flow_oi_change_5m": 0.0,
        "fut_flow_oi_rel_20": 1.0,
        "fut_flow_oi_zscore_20": 0.0,
        "opt_flow_atm_strike": 50000.0,
        "opt_flow_rows": 3.0,
        "opt_flow_ce_oi_total": 10000.0,
        "opt_flow_pe_oi_total": 10000.0,
        "opt_flow_ce_volume_total": 1000.0,
        "opt_flow_pe_volume_total": 1000.0,
        "opt_flow_pcr_oi": 1.0,
        "pcr_change_5m": 0.02,
        "pcr_change_15m": 0.03,
        "opt_flow_atm_call_return_1m": 0.0,
        "opt_flow_atm_put_return_1m": 0.0,
        "opt_flow_atm_oi_change_1m": 0.0,
        "atm_oi_ratio": 0.5,
        "near_atm_oi_ratio": 0.49,
        "opt_flow_ce_pe_oi_diff": 0.0,
        "opt_flow_ce_pe_volume_diff": 0.0,
        "opt_flow_options_volume_total": 2000.0,
        "opt_flow_rel_volume_20": 1.0,
        "time_minute_of_day": 570,
        "time_day_of_week": 0,
        "time_minute_index": 15,
        "ctx_opening_range_ready": 1,
        "ctx_opening_range_breakout_up": 0,
        "ctx_opening_range_breakout_down": 0,
        "ctx_dte_days": 2,
        "ctx_is_expiry_day": 0,
        "ctx_is_near_expiry": 0,
        "ctx_is_high_vix_day": 0,
        "ctx_regime_atr_high": 0,
        "ctx_regime_atr_low": 0,
        "ctx_regime_trend_up": 1,
        "ctx_regime_trend_down": 0,
        "ctx_regime_expiry_near": 0,
    }

    report = validate_snapshot_ml_flat_rows([row], raise_on_error=False)

    assert report["ok"] is True
    assert report["error_count"] == 0


def test_snapshot_ml_flat_tier2_allows_atm_shift_nulls_when_continuity_breaks() -> None:
    base = {
        "trade_date": "2026-03-16",
        "year": 2026,
        "instrument": "BANKNIFTY26MARFUT",
        "schema_name": "SnapshotMLFlat",
        "schema_version": "3.0",
        "build_source": "historical",
        "build_run_id": "test_run",
        "px_fut_open": 50000.0,
        "px_fut_high": 50010.0,
        "px_fut_low": 49990.0,
        "px_fut_close": 50005.0,
        "px_spot_open": 49950.0,
        "px_spot_high": 49960.0,
        "px_spot_low": 49940.0,
        "px_spot_close": 49955.0,
        "ret_1m": 0.0,
        "ret_3m": 0.0,
        "ret_5m": 0.0,
        "ema_9": 50000.0,
        "ema_21": 50000.0,
        "ema_50": 50000.0,
        "ema_9_21_spread": 0.0,
        "ema_9_slope": 0.0,
        "ema_21_slope": 0.0,
        "ema_50_slope": 0.0,
        "osc_rsi_14": 50.0,
        "osc_atr_14": 100.0,
        "osc_atr_ratio": 0.002,
        "osc_atr_percentile": 0.5,
        "osc_atr_daily_percentile": 0.5,
        "vwap_fut": 50002.0,
        "vwap_distance": 0.0,
        "dist_from_day_high": -0.0001,
        "dist_from_day_low": 0.0001,
        "dist_basis": 50.0,
        "dist_basis_change_1m": 0.0,
        "fut_flow_volume": 1000.0,
        "fut_flow_oi": 2000.0,
        "fut_flow_rel_volume_20": 1.0,
        "fut_flow_volume_accel_1m": 0.0,
        "fut_flow_oi_change_1m": 0.0,
        "fut_flow_oi_change_5m": 0.0,
        "fut_flow_oi_rel_20": 1.0,
        "fut_flow_oi_zscore_20": 0.0,
        "opt_flow_rows": 3.0,
        "opt_flow_ce_oi_total": 10000.0,
        "opt_flow_pe_oi_total": 10000.0,
        "opt_flow_ce_volume_total": 1000.0,
        "opt_flow_pe_volume_total": 1000.0,
        "opt_flow_pcr_oi": 1.0,
        "pcr_change_5m": 0.02,
        "pcr_change_15m": 0.03,
        "atm_oi_ratio": 0.5,
        "near_atm_oi_ratio": 0.49,
        "opt_flow_ce_pe_oi_diff": 0.0,
        "opt_flow_ce_pe_volume_diff": 0.0,
        "opt_flow_options_volume_total": 2000.0,
        "opt_flow_rel_volume_20": 1.0,
        "time_minute_of_day": 570,
        "time_day_of_week": 0,
        "ctx_opening_range_ready": 1,
        "ctx_opening_range_breakout_up": 0,
        "ctx_opening_range_breakout_down": 0,
        "ctx_dte_days": 2,
        "ctx_is_expiry_day": 0,
        "ctx_is_near_expiry": 0,
        "ctx_is_high_vix_day": 0,
        "ctx_regime_atr_high": 0,
        "ctx_regime_atr_low": 0,
        "ctx_regime_trend_up": 1,
        "ctx_regime_trend_down": 0,
        "ctx_regime_expiry_near": 0,
    }
    rows = []
    for idx, (ts, strike, call_ret) in enumerate(
        [
            ("2026-03-16T09:15:00+05:30", 50000.0, None),
            ("2026-03-16T09:16:00+05:30", 50100.0, None),
            ("2026-03-16T09:17:00+05:30", 50100.0, 0.01),
        ]
    ):
        row = dict(base)
        row["timestamp"] = ts
        row["snapshot_id"] = f"20260316_{915 + idx}"
        row["time_minute_index"] = idx
        row["opt_flow_atm_strike"] = strike
        row["opt_flow_atm_call_return_1m"] = call_ret
        row["opt_flow_atm_put_return_1m"] = call_ret
        row["opt_flow_atm_oi_change_1m"] = 10.0 if call_ret is not None else None
        rows.append(row)

    report = validate_snapshot_ml_flat_rows(rows, raise_on_error=False)

    assert report["ok"] is True
    assert report["error_count"] == 0


def test_tier2_allows_column_threshold_override_for_atm_continuity() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2020-03-27",
                "time_minute_index": idx,
                "opt_flow_atm_strike": 50000.0,
                "opt_flow_rows": 3.0,
                "opt_flow_atm_call_return_1m": (0.01 if idx < 9 else None),
            }
            for idx in range(10)
        ]
    )

    rules = {
        "tier2": {
            "columns": ["opt_flow_atm_call_return_1m"],
            "per_day_completeness_min_after_warmup": 0.95,
            "per_day_completeness_min_after_warmup_by_column": {
                "opt_flow_atm_call_return_1m": 0.80,
            },
            "warmup_bars_by_column": {
                "opt_flow_atm_call_return_1m": 1,
            },
        }
    }

    errors = _check_tier2(frame, rules)

    assert errors == []
