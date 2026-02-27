from typing import List, Sequence, Tuple


FEATURE_PROFILE_ALL = "all"
FEATURE_PROFILE_FUTURES_OPTIONS_ONLY = "futures_options_only"
FEATURE_PROFILE_CORE_V1 = "core_v1"
FEATURE_PROFILE_CORE_V2 = "core_v2"
FEATURE_PROFILES: Tuple[str, ...] = (
    FEATURE_PROFILE_ALL,
    FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    FEATURE_PROFILE_CORE_V1,
    FEATURE_PROFILE_CORE_V2,
)


_CORE_V1_EXACT = {
    "ret_1m",
    "ret_5m",
    "vwap_distance",
    "distance_from_day_high",
    "distance_from_day_low",
    "opening_range_breakout_up",
    "opening_range_breakout_down",
    "rsi_14",
    "ema_9_21_spread",
    "atr_ratio",
    "atm_call_return_1m",
    "atm_put_return_1m",
    "atm_oi_change_1m",
    "pcr_oi",
    "ce_pe_oi_diff",
    "ce_pe_volume_diff",
    "fut_rel_volume_20",
    "fut_volume_accel_1m",
    "options_volume_total",
    "options_rel_volume_20",
    "minute_of_day",
    "day_of_week",
    # Optional regime/options features for future extension.
    "dte_days",
    "vix_prev_close",
    "vix_prev_close_change_1d",
    "vix_prev_close_zscore_20d",
    "atm_iv",
    "iv_skew",
}


_CORE_V2_ADDITIONS = {
    "dte_days",
    "is_expiry_day",
    "is_near_expiry",
    "vix_prev_close",
    "vix_prev_close_change_1d",
    "vix_prev_close_zscore_20d",
    "is_high_vix_day",
    "regime_vol_high",
    "regime_vol_low",
    "regime_vol_neutral",
    "regime_atr_high",
    "regime_atr_low",
    "regime_trend_up",
    "regime_trend_down",
    "regime_expiry_near",
    "atr_daily_percentile",
}


# Columns that must never be model features regardless of profile.
# Includes forward-looking label/return cols (base + all horizon suffixes).
_ALWAYS_EXCLUDED_PREFIXES = (
    "ce_label", "pe_label",
    "ce_forward_return", "pe_forward_return",
    "ce_path_exit_reason", "pe_path_exit_reason",
)
# Unnormalized price levels that pollute the feature space under profile='all'.
_PRICE_LEVEL_COLS = frozenset({"opening_range_high", "opening_range_low"})


def _is_always_excluded(col: str) -> bool:
    c = str(col)
    if c in _PRICE_LEVEL_COLS:
        return True
    return any(c == p or c.startswith(f"{p}_") for p in _ALWAYS_EXCLUDED_PREFIXES)


def apply_feature_profile(columns: Sequence[str], feature_profile: str) -> List[str]:
    profile = str(feature_profile or FEATURE_PROFILE_ALL).strip().lower()
    cols = [str(c) for c in columns]

    if profile == FEATURE_PROFILE_ALL:
        # Exclude label/return columns and unnormalized price levels even under 'all'.
        return [c for c in cols if not _is_always_excluded(c)]

    if profile == FEATURE_PROFILE_FUTURES_OPTIONS_ONLY:
        blocked_prefixes = ("spot_", "depth_")
        blocked_exact = {"basis", "basis_change_1m"}
        return [col for col in cols if not col.startswith(blocked_prefixes) and col not in blocked_exact]

    if profile == FEATURE_PROFILE_CORE_V1:
        return [col for col in cols if col in _CORE_V1_EXACT]
    if profile == FEATURE_PROFILE_CORE_V2:
        allowed = _CORE_V1_EXACT.union(_CORE_V2_ADDITIONS)
        return [col for col in cols if col in allowed]

    raise ValueError(f"unsupported feature_profile: {feature_profile}")
