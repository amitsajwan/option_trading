from __future__ import annotations

from typing import Any

from .velocity_features import VELOCITY_COLUMNS


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

_V2_ADDITIONAL_ENRICHMENT_FIELDS: tuple[str, ...] = (
    "adx_14",
    "vol_spike_ratio",
    "ctx_gap_pct",
    "ctx_gap_up",
    "ctx_gap_down",
)
_V2_ENRICHMENT_FIELDS: tuple[str, ...] = tuple((*VELOCITY_COLUMNS, *_V2_ADDITIONAL_ENRICHMENT_FIELDS))

_STAGE_FIELD_SPECS: dict[str, dict[str | None, tuple[str, ...]]] = {
    "stage1_entry_view": {
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
    "stage2_direction_view": {
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
        # Regime flags — valid on ALL rows (daily regime, not time-of-day dependent).
        # Present in snapshots_ml_flat_v2 at 0% missing rate.
        # NOTE: for live inference via _project_view(snapshot), verify the snapshot
        # block key that carries these fields before enabling in production.
        "regime_context": (
            "ctx_regime_atr_high",
            "ctx_regime_atr_low",
            "ctx_regime_trend_up",
            "ctx_regime_trend_down",
            "ctx_regime_expiry_near",
            "ctx_is_high_vix_day",
        ),
        "chain_aggregates": (
            "pcr",
            "pcr_change_5m",
            "pcr_change_15m",
            "pcr_change_30m",
            "max_pain",
            "ce_pe_oi_diff",
            "ce_pe_volume_diff",
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
            "atm_ce_return_1m",
            "atm_pe_return_1m",
            "atm_ce_oi",
            "atm_pe_oi",
            "atm_ce_oi_change_1m",
            "atm_pe_oi_change_1m",
            "atm_ce_oi_change_30m",
            "atm_pe_oi_change_30m",
            "atm_ce_iv",
            "atm_pe_iv",
            "atm_ce_pe_price_diff",
            "atm_ce_pe_iv_diff",
            "atm_oi_ratio",
        ),
        "iv_derived": (
            "iv_skew",
            "iv_skew_dir",
            "iv_percentile",
            "iv_regime",
        ),
    },
    "stage3_recipe_view": {
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


def _extend_spec_with_v2_enrichment(view_name: str) -> dict[str | None, tuple[str, ...]]:
    base_spec = _STAGE_FIELD_SPECS[view_name]
    extended = {block_name: tuple(field_names) for block_name, field_names in base_spec.items()}
    extended["velocity_enrichment"] = _V2_ENRICHMENT_FIELDS
    return extended


_STAGE_FIELD_SPECS.update(
    {
        "stage1_entry_view_v2": _extend_spec_with_v2_enrichment("stage1_entry_view"),
        "stage2_direction_view_v2": _extend_spec_with_v2_enrichment("stage2_direction_view"),
        "stage3_recipe_view_v2": _extend_spec_with_v2_enrichment("stage3_recipe_view"),
    }
)


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


def _project_view_from_flat_row(row: dict[str, Any], view_name: str) -> dict[str, Any]:
    work = row if isinstance(row, dict) else {}
    spec = _STAGE_FIELD_SPECS[view_name]
    out: dict[str, Any] = {"view_name": view_name}

    for field_name in spec.get(None, ()):
        out[field_name] = work.get(field_name)

    for block_name, field_names in spec.items():
        if block_name is None:
            continue
        for field_name in field_names:
            out[field_name] = work.get(field_name)
    return out


def project_stage1_entry_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _project_view(snapshot, "stage1_entry_view")


def project_stage2_direction_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _project_view(snapshot, "stage2_direction_view")


def project_stage3_recipe_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _project_view(snapshot, "stage3_recipe_view")


def project_stage_views(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "stage1_entry_view": project_stage1_entry_view(snapshot),
        "stage2_direction_view": project_stage2_direction_view(snapshot),
        "stage3_recipe_view": project_stage3_recipe_view(snapshot),
    }


def project_stage1_entry_view_v2(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _project_view(snapshot, "stage1_entry_view_v2")


def project_stage2_direction_view_v2(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _project_view(snapshot, "stage2_direction_view_v2")


def project_stage3_recipe_view_v2(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _project_view(snapshot, "stage3_recipe_view_v2")


def project_stage_views_v2(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "stage1_entry_view_v2": project_stage1_entry_view_v2(snapshot),
        "stage2_direction_view_v2": project_stage2_direction_view_v2(snapshot),
        "stage3_recipe_view_v2": project_stage3_recipe_view_v2(snapshot),
    }


def project_stage1_entry_view_from_flat_row(row: dict[str, Any]) -> dict[str, Any]:
    return _project_view_from_flat_row(row, "stage1_entry_view")


def project_stage2_direction_view_from_flat_row(row: dict[str, Any]) -> dict[str, Any]:
    return _project_view_from_flat_row(row, "stage2_direction_view")


def project_stage3_recipe_view_from_flat_row(row: dict[str, Any]) -> dict[str, Any]:
    return _project_view_from_flat_row(row, "stage3_recipe_view")


def project_stage_views_from_flat_row(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "stage1_entry_view": project_stage1_entry_view_from_flat_row(row),
        "stage2_direction_view": project_stage2_direction_view_from_flat_row(row),
        "stage3_recipe_view": project_stage3_recipe_view_from_flat_row(row),
    }


def project_stage1_entry_view_v2_from_flat_row(row: dict[str, Any]) -> dict[str, Any]:
    return _project_view_from_flat_row(row, "stage1_entry_view_v2")


def project_stage2_direction_view_v2_from_flat_row(row: dict[str, Any]) -> dict[str, Any]:
    return _project_view_from_flat_row(row, "stage2_direction_view_v2")


def project_stage3_recipe_view_v2_from_flat_row(row: dict[str, Any]) -> dict[str, Any]:
    return _project_view_from_flat_row(row, "stage3_recipe_view_v2")


def project_stage_views_v2_from_flat_row(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "stage1_entry_view_v2": project_stage1_entry_view_v2_from_flat_row(row),
        "stage2_direction_view_v2": project_stage2_direction_view_v2_from_flat_row(row),
        "stage3_recipe_view_v2": project_stage3_recipe_view_v2_from_flat_row(row),
    }


__all__ = [
    "project_stage1_entry_view",
    "project_stage2_direction_view",
    "project_stage3_recipe_view",
    "project_stage_views",
    "project_stage1_entry_view_v2",
    "project_stage2_direction_view_v2",
    "project_stage3_recipe_view_v2",
    "project_stage_views_v2",
    "project_stage1_entry_view_from_flat_row",
    "project_stage2_direction_view_from_flat_row",
    "project_stage3_recipe_view_from_flat_row",
    "project_stage_views_from_flat_row",
    "project_stage1_entry_view_v2_from_flat_row",
    "project_stage2_direction_view_v2_from_flat_row",
    "project_stage3_recipe_view_v2_from_flat_row",
    "project_stage_views_v2_from_flat_row",
]
