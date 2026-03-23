from __future__ import annotations

from typing import List, Sequence

from ..contracts.types import (
    FEATURE_PROFILE_ALL,
    FEATURE_PROFILE_CORE_V1,
    FEATURE_PROFILE_CORE_V2,
    FEATURE_PROFILE_FUTURES_CORE,
    FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    FEATURE_PROFILES,
)


_CORE_V1_EXACT = {
    "ret_1m",
    "ret_5m",
    "vwap_distance",
    "dist_from_day_high",
    "dist_from_day_low",
    "ctx_opening_range_breakout_up",
    "ctx_opening_range_breakout_down",
    "osc_rsi_14",
    "ema_9_21_spread",
    "osc_atr_ratio",
    "opt_flow_atm_call_return_1m",
    "opt_flow_atm_put_return_1m",
    "opt_flow_atm_oi_change_1m",
    "opt_flow_pcr_oi",
    "opt_flow_ce_pe_oi_diff",
    "opt_flow_ce_pe_volume_diff",
    "fut_flow_rel_volume_20",
    "fut_flow_volume_accel_1m",
    "opt_flow_options_volume_total",
    "opt_flow_rel_volume_20",
    "time_minute_of_day",
    "time_day_of_week",
    "ctx_dte_days",
    "ctx_is_high_vix_day",
}

_CORE_V2_ADDITIONS = {
    "ctx_dte_days",
    "ctx_is_expiry_day",
    "ctx_is_near_expiry",
    "ctx_is_high_vix_day",
    "ctx_regime_atr_high",
    "ctx_regime_atr_low",
    "ctx_regime_trend_up",
    "ctx_regime_trend_down",
    "ctx_regime_expiry_near",
    "osc_atr_daily_percentile",
}

_FUTURES_CORE = frozenset(
    {
        "ret_1m",
        "ret_3m",
        "ret_5m",
        "ema_9_21_spread",
        "ema_9_slope",
        "ema_21_slope",
        "ema_50_slope",
        "osc_rsi_14",
        "osc_atr_ratio",
        "osc_atr_daily_percentile",
        "vwap_distance",
        "dist_from_day_high",
        "dist_from_day_low",
        "ctx_opening_range_breakout_up",
        "ctx_opening_range_breakout_down",
        "ctx_opening_range_ready",
        "time_minute_of_day",
        "time_day_of_week",
        "fut_flow_rel_volume_20",
        "fut_flow_volume_accel_1m",
        "fut_flow_oi_change_1m",
        "fut_flow_oi_change_5m",
        "fut_flow_oi_rel_20",
        "fut_flow_oi_zscore_20",
        "dist_basis",
        "dist_basis_change_1m",
        "ctx_dte_days",
        "ctx_is_expiry_day",
        "ctx_is_near_expiry",
        "ctx_is_high_vix_day",
        "ctx_regime_trend_up",
        "ctx_regime_trend_down",
        "ctx_regime_atr_high",
        "ctx_regime_atr_low",
        "ctx_regime_expiry_near",
    }
)

_ALWAYS_EXCLUDED_PREFIXES = (
    "ce_label",
    "pe_label",
    "ce_forward_return",
    "pe_forward_return",
    "ce_path_exit_reason",
    "pe_path_exit_reason",
    "long_label",
    "short_label",
    "long_forward_return",
    "short_forward_return",
    "long_path_exit_reason",
    "short_path_exit_reason",
    "move_label",
    "move_path_exit_reason",
    "move_first_hit_side",
    "move_event_end_ts",
    "long",
    "short",
)
_PRICE_LEVEL_COLS = frozenset({"opening_range_high", "opening_range_low"})


def is_feature_excluded(col: str) -> bool:
    name = str(col)
    if name in _PRICE_LEVEL_COLS:
        return True
    return any(name == prefix or name.startswith(f"{prefix}_") for prefix in _ALWAYS_EXCLUDED_PREFIXES)


def apply_feature_profile(columns: Sequence[str], feature_profile: str) -> List[str]:
    profile = str(feature_profile or FEATURE_PROFILE_ALL).strip().lower()
    cols = [str(col) for col in columns]
    if profile == FEATURE_PROFILE_ALL:
        return [col for col in cols if not is_feature_excluded(col)]
    if profile == FEATURE_PROFILE_FUTURES_OPTIONS_ONLY:
        blocked_prefixes = ("spot_", "depth_", "px_spot_")
        blocked_exact = {"basis", "basis_change_1m", "dist_basis", "dist_basis_change_1m"}
        return [
            col
            for col in cols
            if not col.startswith(blocked_prefixes)
            and col not in blocked_exact
            and not is_feature_excluded(col)
        ]
    if profile == FEATURE_PROFILE_CORE_V1:
        return [col for col in cols if col in _CORE_V1_EXACT]
    if profile == FEATURE_PROFILE_CORE_V2:
        allowed = _CORE_V1_EXACT.union(_CORE_V2_ADDITIONS)
        return [col for col in cols if col in allowed]
    if profile == FEATURE_PROFILE_FUTURES_CORE:
        return [col for col in cols if col in _FUTURES_CORE]
    raise ValueError(f"unsupported feature_profile: {feature_profile}")
