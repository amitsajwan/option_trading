from __future__ import annotations

from typing import Any


_COMMON_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "snapshot_id",
    "instrument",
    "trade_date",
    "timestamp",
    "schema_name",
    "schema_version",
)

_COMMON_SESSION_FIELDS: tuple[str, ...] = (
    "minutes_since_open",
    "minutes_to_close",
    "day_of_week",
    "days_to_expiry",
    "is_expiry_day",
    "session_phase",
    "is_first_hour",
    "is_last_hour",
)

_STAGE_FIELD_SPECS: dict[str, dict[str | None, tuple[str, ...]]] = {
    "stage1_entry": {
        None: _COMMON_TOP_LEVEL_FIELDS,
        "session_context": _COMMON_SESSION_FIELDS,
        "futures_derived": (
            "fut_return_1m",
            "fut_return_3m",
            "fut_return_5m",
            "realized_vol_30m",
            "vol_ratio",
            "fut_volume_ratio",
            "fut_oi_change_30m",
            "ema_9_slope",
            "ema_21_slope",
            "ema_50_slope",
            "price_vs_vwap",
            "atr_ratio",
            "atr_daily_percentile",
            "dist_from_day_high",
            "dist_from_day_low",
        ),
        "mtf_derived": (
            "rsi_14_1m",
            "rsi_14_5m",
            "atr_14_1m",
            "bb_width_5m",
            "mtf_aligned",
        ),
        "opening_range": (
            "opening_range_ready",
            "or_width_pct",
            "price_vs_orh",
            "price_vs_orl",
            "orh_broken",
            "orl_broken",
            "bars_since_or_break_up",
            "bars_since_or_break_down",
        ),
        "vix_context": (
            "vix_current",
            "vix_intraday_chg",
            "vix_regime",
            "vix_spike_flag",
        ),
        "chain_aggregates": (
            "pcr",
            "pcr_change_30m",
            "ce_pe_oi_diff",
            "ce_pe_volume_diff",
            "atm_straddle_pct",
            "distance_to_max_pain_pct",
        ),
        "ladder_aggregates": (
            "near_atm_pcr",
            "near_atm_oi_concentration",
            "near_atm_volume_concentration",
        ),
    },
    "stage2_direction": {
        None: _COMMON_TOP_LEVEL_FIELDS,
        "session_context": _COMMON_SESSION_FIELDS,
        "futures_derived": (
            "fut_return_1m",
            "fut_return_3m",
            "fut_return_5m",
            "fut_return_15m",
            "ema_9",
            "ema_21",
            "ema_50",
            "ema_9_slope",
            "ema_21_slope",
            "ema_50_slope",
            "vwap",
            "price_vs_vwap",
            "dist_from_day_high",
            "dist_from_day_low",
        ),
        "mtf_derived": (
            "rsi_14_1m",
            "rsi_14_5m",
            "rsi_14_15m",
            "macd_line_5m",
            "macd_signal_5m",
            "macd_hist_5m",
            "ema_trend_5m",
            "ema_trend_15m",
            "mtf_aligned",
        ),
        "opening_range": (
            "price_vs_orh",
            "price_vs_orl",
            "orh_broken",
            "orl_broken",
            "bars_since_or_break_up",
            "bars_since_or_break_down",
        ),
        "vix_context": (
            "vix_current",
            "vix_intraday_chg",
            "vix_regime",
        ),
        "chain_aggregates": (
            "pcr",
            "pcr_change_30m",
            "max_pain",
            "ce_pe_oi_diff",
            "ce_pe_volume_diff",
            "distance_to_max_pain_pct",
        ),
        "ladder_aggregates": (
            "near_atm_pcr",
            "near_atm_oi_concentration",
            "near_atm_volume_concentration",
            "oi_sum_m3_p3_ce",
            "oi_sum_m3_p3_pe",
            "vol_sum_m3_p3_ce",
            "vol_sum_m3_p3_pe",
        ),
        "atm_options": (
            "atm_ce_return_1m",
            "atm_pe_return_1m",
            "atm_ce_oi_change_1m",
            "atm_pe_oi_change_1m",
            "atm_ce_oi_change_30m",
            "atm_pe_oi_change_30m",
            "atm_ce_iv",
            "atm_pe_iv",
            "atm_ce_pe_price_diff",
            "atm_ce_pe_iv_diff",
        ),
        "iv_derived": (
            "iv_skew",
            "iv_skew_dir",
            "iv_percentile",
            "iv_regime",
        ),
    },
    "stage3_recipe": {
        None: _COMMON_TOP_LEVEL_FIELDS,
        "session_context": _COMMON_SESSION_FIELDS,
        "futures_derived": (
            "realized_vol_30m",
            "vol_ratio",
            "atr_ratio",
            "atr_daily_percentile",
            "dist_from_day_high",
            "dist_from_day_low",
        ),
        "mtf_derived": (
            "atr_14_1m",
            "atr_14_5m",
            "atr_14_15m",
            "bb_width_5m",
            "bb_pct_b_5m",
            "mtf_aligned",
        ),
        "opening_range": (
            "opening_range_ready",
            "or_width_pct",
            "orh_broken",
            "orl_broken",
            "bars_since_or_break_up",
            "bars_since_or_break_down",
        ),
        "vix_context": (
            "vix_current",
            "vix_intraday_chg",
            "vix_regime",
            "vix_spike_flag",
        ),
        "chain_aggregates": (
            "pcr",
            "atm_straddle_price",
            "atm_straddle_pct",
            "distance_to_max_pain_pct",
        ),
        "ladder_aggregates": (
            "near_atm_pcr",
            "near_atm_oi_concentration",
            "near_atm_volume_concentration",
            "oi_sum_m3_p3_ce",
            "oi_sum_m3_p3_pe",
            "vol_sum_m3_p3_ce",
            "vol_sum_m3_p3_pe",
        ),
        "atm_options": (
            "atm_ce_return_1m",
            "atm_pe_return_1m",
            "atm_ce_oi_change_1m",
            "atm_pe_oi_change_1m",
            "atm_ce_iv",
            "atm_pe_iv",
            "atm_ce_pe_price_diff",
            "atm_ce_pe_iv_diff",
        ),
        "iv_derived": (
            "iv_skew",
            "iv_skew_dir",
            "iv_percentile",
            "iv_regime",
            "iv_expiry_type",
        ),
    },
}


def _project_view(snapshot: dict[str, Any], view_name: str) -> dict[str, Any]:
    work = snapshot if isinstance(snapshot, dict) else {}
    spec = _STAGE_FIELD_SPECS[view_name]
    out: dict[str, Any] = {"view_name": view_name}

    for field_name in spec.get(None, ()):
        out[field_name] = work.get(field_name)

    for block_name, field_names in spec.items():
        if block_name is None:
            continue
        block = work.get(block_name) if isinstance(work.get(block_name), dict) else {}
        for field_name in field_names:
            out[field_name] = block.get(field_name)
    return out


def project_stage1_entry_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _project_view(snapshot, "stage1_entry")


def project_stage2_direction_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _project_view(snapshot, "stage2_direction")


def project_stage3_recipe_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _project_view(snapshot, "stage3_recipe")


def project_stage_views(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "stage1_entry": project_stage1_entry_view(snapshot),
        "stage2_direction": project_stage2_direction_view(snapshot),
        "stage3_recipe": project_stage3_recipe_view(snapshot),
    }


__all__ = [
    "project_stage1_entry_view",
    "project_stage2_direction_view",
    "project_stage3_recipe_view",
    "project_stage_views",
]
