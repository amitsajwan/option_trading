from __future__ import annotations

from typing import Any


CONTRACT_ID = "market_snapshot_final"
SCHEMA_NAME = "MarketSnapshot"
SCHEMA_VERSION = "3.0"

REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_name",
    "schema_version",
    "snapshot_id",
    "instrument",
    "trade_date",
    "timestamp",
    "session_context",
    "futures_bar",
    "futures_derived",
    "mtf_derived",
    "opening_range",
    "vix_context",
    "strikes",
    "chain_aggregates",
    "ladder_aggregates",
    "atm_options",
    "iv_derived",
    "session_levels",
)

REQUIRED_BLOCK_FIELDS: dict[str, tuple[str, ...]] = {
    "session_context": (
        "snapshot_id",
        "timestamp",
        "date",
        "time",
        "minutes_since_open",
        "minutes_to_close",
        "day_of_week",
        "days_to_expiry",
        "is_expiry_day",
        "session_phase",
        "is_first_hour",
        "is_last_hour",
    ),
    "futures_bar": ("fut_open", "fut_high", "fut_low", "fut_close", "fut_volume", "fut_oi"),
    "futures_derived": (
        "fut_return_1m",
        "fut_return_3m",
        "fut_return_5m",
        "fut_return_15m",
        "fut_return_30m",
        "realized_vol_30m",
        "vol_ratio",
        "fut_volume_ratio",
        "fut_oi_change_30m",
        "ema_9",
        "ema_21",
        "ema_50",
        "ema_9_slope",
        "ema_21_slope",
        "ema_50_slope",
        "vwap",
        "price_vs_vwap",
        "atr_ratio",
        "atr_daily_percentile",
        "dist_from_day_high",
        "dist_from_day_low",
    ),
    "mtf_derived": (
        "rsi_14_1m",
        "rsi_14_5m",
        "rsi_14_15m",
        "ema_9_5m",
        "ema_21_5m",
        "ema_50_5m",
        "ema_9_15m",
        "ema_21_15m",
        "ema_50_15m",
        "macd_line_5m",
        "macd_signal_5m",
        "macd_hist_5m",
        "atr_14_1m",
        "atr_14_5m",
        "atr_14_15m",
        "bb_upper_5m",
        "bb_lower_5m",
        "bb_width_5m",
        "bb_pct_b_5m",
        "ema_trend_5m",
        "ema_trend_15m",
        "mtf_aligned",
    ),
    "opening_range": (
        "orh",
        "orl",
        "or_width",
        "or_width_pct",
        "price_vs_orh",
        "price_vs_orl",
        "opening_range_ready",
        "orh_broken",
        "orl_broken",
        "bars_since_or_break_up",
        "bars_since_or_break_down",
    ),
    "vix_context": ("vix_current", "vix_prev_close", "vix_intraday_chg", "vix_regime", "vix_spike_flag"),
    "chain_aggregates": (
        "atm_strike",
        "strike_count",
        "total_ce_oi",
        "total_pe_oi",
        "total_ce_volume",
        "total_pe_volume",
        "pcr",
        "pcr_change_5m",
        "pcr_change_15m",
        "pcr_change_30m",
        "max_pain",
        "ce_oi_top_strike",
        "pe_oi_top_strike",
        "ce_pe_oi_diff",
        "ce_pe_volume_diff",
        "atm_straddle_price",
        "atm_straddle_pct",
        "distance_to_max_pain_pct",
    ),
    "ladder_aggregates": (
        "near_atm_pcr",
        "near_atm_oi_ratio",
        "near_atm_oi_concentration",
        "near_atm_volume_concentration",
        "oi_sum_m3_p3_ce",
        "oi_sum_m3_p3_pe",
        "vol_sum_m3_p3_ce",
        "vol_sum_m3_p3_pe",
    ),
    "atm_options": (
        "atm_ce_strike",
        "atm_ce_open",
        "atm_ce_high",
        "atm_ce_low",
        "atm_ce_close",
        "atm_ce_return_1m",
        "atm_ce_volume",
        "atm_ce_oi",
        "atm_ce_oi_change_1m",
        "atm_ce_oi_change_30m",
        "atm_ce_iv",
        "atm_ce_vol_ratio",
        "atm_pe_strike",
        "atm_pe_open",
        "atm_pe_high",
        "atm_pe_low",
        "atm_pe_close",
        "atm_pe_return_1m",
        "atm_pe_volume",
        "atm_pe_oi",
        "atm_pe_oi_change_1m",
        "atm_pe_oi_change_30m",
        "atm_pe_iv",
        "atm_pe_vol_ratio",
        "atm_ce_pe_price_diff",
        "atm_ce_pe_iv_diff",
        "atm_oi_ratio",
    ),
    "iv_derived": ("iv_skew", "iv_skew_dir", "iv_percentile", "iv_regime", "iv_expiry_type"),
    "session_levels": (
        "prev_day_high",
        "prev_day_low",
        "prev_day_close",
        "week_high",
        "week_low",
        "overnight_gap",
        "prev_day_pcr",
        "prev_day_max_pain",
    ),
}

REQUIRED_STRIKE_FIELDS = (
    "strike",
    "ce_ltp",
    "pe_ltp",
    "ce_oi",
    "pe_oi",
    "ce_volume",
    "pe_volume",
    "ce_iv",
    "pe_iv",
    "ce_open",
    "ce_high",
    "ce_low",
    "pe_open",
    "pe_high",
    "pe_low",
)


def _ensure_dict(payload: Any, field_name: str, errors: list[str]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    errors.append(f"{field_name} must be an object")
    return {}


def validate_market_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    work = snapshot if isinstance(snapshot, dict) else {}
    if not isinstance(snapshot, dict):
        errors.append("snapshot must be an object")

    for field_name in REQUIRED_TOP_LEVEL_FIELDS:
        if field_name not in work:
            errors.append(f"missing top-level field: {field_name}")

    schema_name = str(work.get("schema_name") or "").strip()
    schema_version = str(work.get("schema_version") or "").strip()
    if schema_name != SCHEMA_NAME:
        errors.append(f"schema_name={schema_name!r} expected={SCHEMA_NAME!r}")
    if schema_version != SCHEMA_VERSION:
        errors.append(f"schema_version={schema_version!r} expected={SCHEMA_VERSION!r}")

    snapshot_id = str(work.get("snapshot_id") or "").strip()
    instrument = str(work.get("instrument") or "").strip()
    trade_date = str(work.get("trade_date") or "").strip()
    timestamp = str(work.get("timestamp") or "").strip()
    if not snapshot_id:
        errors.append("snapshot_id must be non-empty")
    if not instrument:
        errors.append("instrument must be non-empty")
    if not trade_date:
        errors.append("trade_date must be non-empty")
    if not timestamp:
        errors.append("timestamp must be non-empty")

    for block_name, field_names in REQUIRED_BLOCK_FIELDS.items():
        block = _ensure_dict(work.get(block_name), block_name, errors)
        for field_name in field_names:
            if field_name not in block:
                errors.append(f"{block_name}.{field_name} missing")

    strikes = work.get("strikes")
    if not isinstance(strikes, list):
        errors.append("strikes must be a list")
    else:
        for idx, row in enumerate(strikes):
            if not isinstance(row, dict):
                errors.append(f"strikes[{idx}] must be an object")
                continue
            for field_name in REQUIRED_STRIKE_FIELDS:
                if field_name not in row:
                    errors.append(f"strikes[{idx}].{field_name} missing")

    report = {
        "ok": len(errors) == 0,
        "schema_name": schema_name,
        "schema_version": schema_version,
        "error_count": len(errors),
        "errors": errors,
    }
    if errors and raise_on_error:
        preview = "; ".join(errors[:5])
        more = "" if len(errors) <= 5 else f"; ... (+{len(errors) - 5} more)"
        raise ValueError(f"market snapshot validation failed: {preview}{more}")
    return report


__all__ = [
    "CONTRACT_ID",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "REQUIRED_TOP_LEVEL_FIELDS",
    "REQUIRED_BLOCK_FIELDS",
    "REQUIRED_STRIKE_FIELDS",
    "validate_market_snapshot",
]
