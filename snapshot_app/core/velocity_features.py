"""
Intraday velocity feature computation for the Snapshot Enrichment Sprint.

Input:  morning_df — all ml_flat rows for a trade_date from 10:00 to 11:30 (sorted by timestamp)
        midday_snapshot — the 11:30 ml_flat row (pd.Series)
        prev_day_close — previous trading day's closing future price (for gap features)

Output: Dict[str, float] — ~30 velocity/delta features ready to merge onto the 11:30 row.
        Returns all NaN if morning_df has fewer than 3 rows.

Column naming convention:
  vel_   velocity / delta features (signed, can be negative)
  ctx_am_ morning session summary / context features
"""

from __future__ import annotations

import math
from typing import Dict, Optional

import numpy as np
import pandas as pd

# ── column names on snapshots_ml_flat that we read from morning_df ─────────────
_CE_OI = "opt_flow_ce_oi_total"
_PE_OI = "opt_flow_pe_oi_total"
_PCR = "opt_flow_pcr_oi"
_ATM_OI_RATIO = "atm_oi_ratio"
_FUT_CLOSE = "px_fut_close"
_FUT_OPEN = "px_fut_open"
_FUT_HIGH = "px_fut_high"
_FUT_LOW = "px_fut_low"
_CE_VOL = "opt_flow_ce_volume_total"
_PE_VOL = "opt_flow_pe_volume_total"
_VWAP = "vwap_fut"
_PCR_CHG_15M = "pcr_change_15m"

# ── IV columns — present only when morning_df was enriched with raw JSON IV ────
_ATM_CE_IV = "atm_ce_iv"
_ATM_PE_IV = "atm_pe_iv"
_IV_SKEW = "iv_skew"

# ── flat threshold for "trend is flat" classification ─────────────────────────
_TREND_FLAT_SLOPE_ABS = 0.02   # normalised slope below this → ctx_am_trend = 0
_MIN_RANGE_SIZE = 10.0          # futures points — below this clip ctx_am_price_position


def _nan_dict() -> Dict[str, float]:
    """Return a dict with all output columns set to NaN."""
    return {col: float("nan") for col in _ALL_OUTPUT_COLUMNS}


def _safe_float(series: pd.Series, index: int) -> Optional[float]:
    """Return element at positional index as float, or None if missing/NaN."""
    if index < 0 or index >= len(series):
        return None
    val = series.iloc[index]
    if pd.isna(val):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _first(series: pd.Series) -> Optional[float]:
    return _safe_float(series, 0)


def _last(series: pd.Series) -> Optional[float]:
    return _safe_float(series, len(series) - 1)


def _at_offset(series: pd.Series, offset_from_end: int) -> Optional[float]:
    """Return value offset_from_end positions before the last row (0 = last, 1 = second-to-last)."""
    idx = len(series) - 1 - offset_from_end
    return _safe_float(series, idx)


def _linear_slope_normalised(series: pd.Series) -> Optional[float]:
    """
    Least-squares slope of the series (x = 0..n-1), normalised by first value.
    Returns None if fewer than 2 non-NaN points.
    """
    clean = series.dropna()
    if len(clean) < 2:
        return None
    x = np.arange(len(clean), dtype=float)
    y = clean.to_numpy(dtype=float)
    first_val = y[0] if y[0] != 0.0 else 1.0
    slope = float(np.polyfit(x, y, 1)[0])
    return slope / abs(first_val)


def _sign_int(value: Optional[float], threshold: float = 0.0) -> int:
    """Map float to -1 / 0 / +1 with a dead-zone at ±threshold."""
    if value is None or not math.isfinite(value):
        return 0
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def compute_velocity_features(
    morning_df: pd.DataFrame,
    *,
    midday_snapshot: pd.Series,
    prev_day_close: Optional[float] = None,
    prev_day_midday_option_volume: Optional[float] = None,
    avg_20d_midday_option_volume: Optional[float] = None,
) -> Dict[str, float]:
    """
    Compute all velocity/delta features from morning session data.

    Args:
        morning_df:       All ml_flat rows from 10:00–11:30 for one trade_date,
                          sorted ascending by timestamp.  Must already include
                          IV columns (atm_ce_iv, atm_pe_iv, iv_skew) when available
                          — caller merges these from raw JSON before calling.
        midday_snapshot:  The 11:30 ml_flat row (single pd.Series or one-row DataFrame
                          squeezed to Series).  Used for "current" value reads.
        prev_day_close:   Previous day's futures closing price. Required for gap
                          features.
        prev_day_midday_option_volume:
                          Previous trading day's 11:30 total options volume.
                          Used for ctx_am_vol_vs_yday.
        avg_20d_midday_option_volume:
                          Rolling average of prior 20 trading days' 11:30 total
                          options volume. Used for vol_spike_ratio.

    Returns:
        Dict[str, float] — all columns in _ALL_OUTPUT_COLUMNS.
        Returns all NaN when morning_df has fewer than 3 rows.
    """
    if morning_df is None or len(morning_df) < 3:
        return _nan_dict()

    df = morning_df.copy()
    df = df.sort_values("timestamp").reset_index(drop=True)

    # ── parse timestamps ───────────────────────────────────────────────────────
    ts_series = pd.to_datetime(df["timestamp"], errors="coerce")
    ts_open = ts_series.iloc[0]
    ts_midday = pd.to_datetime(midday_snapshot.get("timestamp") if hasattr(midday_snapshot, "get") else midday_snapshot["timestamp"], errors="coerce")
    if pd.isna(ts_open) or pd.isna(ts_midday):
        return _nan_dict()
    minutes_elapsed = max(1.0, (ts_midday - ts_open).total_seconds() / 60.0)

    # ── helper: get numeric series from morning_df ─────────────────────────────
    def col(name: str) -> pd.Series:
        if name not in df.columns:
            return pd.Series([float("nan")] * len(df), dtype=float)
        return pd.to_numeric(df[name], errors="coerce")

    # ── helper: get scalar from midday_snapshot ────────────────────────────────
    def mid(name: str) -> Optional[float]:
        val = midday_snapshot.get(name) if hasattr(midday_snapshot, "get") else (
            midday_snapshot[name] if name in midday_snapshot.index else None
        )
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return _last(col(name))
        try:
            f = float(val)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return _last(col(name))

    out: Dict[str, float] = {}

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP 1 — OI Velocity
    # ═══════════════════════════════════════════════════════════════════════════
    ce_oi = col(_CE_OI)
    pe_oi = col(_PE_OI)
    atm_ratio = col(_ATM_OI_RATIO)

    ce_oi_open = _first(ce_oi)
    pe_oi_open = _first(pe_oi)
    ce_oi_mid = mid(_CE_OI)
    pe_oi_mid = mid(_PE_OI)
    ce_oi_30m_ago = _at_offset(ce_oi, 2)   # 2 × 15-min = 30 min before last
    pe_oi_30m_ago = _at_offset(pe_oi, 2)
    atm_ratio_open = _first(atm_ratio)
    atm_ratio_mid = mid(_ATM_OI_RATIO)
    atm_ratio_30m_ago = _at_offset(atm_ratio, 2)

    def _delta(a: Optional[float], b: Optional[float]) -> float:
        if a is None or b is None:
            return float("nan")
        return b - a

    out["vel_ce_oi_delta_open"] = _delta(ce_oi_open, ce_oi_mid)
    out["vel_pe_oi_delta_open"] = _delta(pe_oi_open, pe_oi_mid)
    out["vel_ce_oi_delta_30m"] = _delta(ce_oi_30m_ago, ce_oi_mid)
    out["vel_pe_oi_delta_30m"] = _delta(pe_oi_30m_ago, pe_oi_mid)
    out["vel_oi_ratio_delta_open"] = _delta(atm_ratio_open, atm_ratio_mid)
    out["vel_oi_ratio_delta_30m"] = _delta(atm_ratio_30m_ago, atm_ratio_mid)

    # build rate = total delta / minutes elapsed
    delta_ce_open = out["vel_ce_oi_delta_open"]
    delta_pe_open = out["vel_pe_oi_delta_open"]
    out["vel_ce_oi_build_rate"] = delta_ce_open / minutes_elapsed if math.isfinite(delta_ce_open) else float("nan")
    out["vel_pe_oi_build_rate"] = delta_pe_open / minutes_elapsed if math.isfinite(delta_pe_open) else float("nan")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP 2 — PCR Velocity
    # ═══════════════════════════════════════════════════════════════════════════
    pcr = col(_PCR)
    pcr_open = _first(pcr)
    pcr_mid = mid(_PCR)
    pcr_30m_ago = _at_offset(pcr, 2)

    out["vel_pcr_delta_open"] = _delta(pcr_open, pcr_mid)
    out["vel_pcr_delta_30m"] = _delta(pcr_30m_ago, pcr_mid)

    # acceleration: last 15m change vs prev 15m change
    pcr_chg_15m_now = mid(_PCR_CHG_15M)
    pcr_chg_15m_prev = _at_offset(col(_PCR_CHG_15M), 1)
    out["vel_pcr_acceleration"] = _delta(pcr_chg_15m_prev, pcr_chg_15m_now)

    # trend direction: sign of normalised slope over all morning PCR rows
    slope = _linear_slope_normalised(pcr.dropna())
    out["vel_pcr_trend_direction"] = float(_sign_int(slope, threshold=_TREND_FLAT_SLOPE_ABS))

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP 3 — Price Velocity
    # ═══════════════════════════════════════════════════════════════════════════
    fut_close = col(_FUT_CLOSE)
    fut_high = col(_FUT_HIGH)
    fut_low = col(_FUT_LOW)

    price_open = _first(col(_FUT_OPEN))       # use open of first bar as "open"
    price_mid = mid(_FUT_CLOSE)
    price_30m_ago = _at_offset(fut_close, 2)
    price_60m_ago = _at_offset(fut_close, 4)
    price_prev_30m_start = _at_offset(fut_close, 3)  # start of prev 30m window

    out["vel_price_delta_open"] = _delta(price_open, price_mid)
    out["vel_price_delta_30m"] = _delta(price_30m_ago, price_mid)
    out["vel_price_delta_60m"] = _delta(price_60m_ago, price_mid)

    # acceleration: last 30m move vs previous 30m move
    prev_30m_move = _delta(price_prev_30m_start, price_30m_ago)
    curr_30m_move = out["vel_price_delta_30m"]
    out["vel_price_acceleration"] = _delta(
        prev_30m_move if math.isfinite(prev_30m_move) else None,
        curr_30m_move if math.isfinite(curr_30m_move) else None,
    )

    # morning range
    range_high = float(fut_high.max()) if fut_high.notna().any() else float("nan")
    range_low = float(fut_low.min()) if fut_low.notna().any() else float("nan")
    out["ctx_am_range_high"] = range_high
    out["ctx_am_range_low"] = range_low

    if math.isfinite(range_high) and math.isfinite(range_low):
        range_size = range_high - range_low
    else:
        range_size = float("nan")
    out["ctx_am_range_size"] = range_size

    # price position in range (0 = at low, 1 = at high)
    if price_mid is not None and math.isfinite(range_size) and range_size >= _MIN_RANGE_SIZE:
        pos = (price_mid - range_low) / range_size
        out["ctx_am_price_position"] = float(np.clip(pos, 0.0, 1.0))
    else:
        out["ctx_am_price_position"] = float("nan")

    # gap features (require prev_day_close)
    if prev_day_close is not None and math.isfinite(prev_day_close) and price_open is not None:
        gap = price_open - prev_day_close
        out["ctx_am_gap_from_yday"] = float(gap)
        gap_pct = gap / prev_day_close if prev_day_close != 0.0 else float("nan")
        out["ctx_gap_pct"] = float(gap_pct) if math.isfinite(gap_pct) else float("nan")
        if math.isfinite(out["ctx_gap_pct"]):
            out["ctx_gap_up"] = float(1 if out["ctx_gap_pct"] > 0.003 else 0)
            out["ctx_gap_down"] = float(1 if out["ctx_gap_pct"] < -0.003 else 0)
        else:
            out["ctx_gap_up"] = float("nan")
            out["ctx_gap_down"] = float("nan")
        # gap filled = price crossed back through prev_day_close by 11:30
        if gap > 0.0:
            # gap up — filled if close dropped back to <= prev_day_close
            out["ctx_am_gap_filled"] = float(1 if (price_mid is not None and price_mid <= prev_day_close) else 0)
        elif gap < 0.0:
            # gap down — filled if close recovered back to >= prev_day_close
            out["ctx_am_gap_filled"] = float(1 if (price_mid is not None and price_mid >= prev_day_close) else 0)
        else:
            out["ctx_am_gap_filled"] = 0.0
    else:
        out["ctx_am_gap_from_yday"] = float("nan")
        out["ctx_am_gap_filled"] = float("nan")
        out["ctx_gap_pct"] = float("nan")
        out["ctx_gap_up"] = float("nan")
        out["ctx_gap_down"] = float("nan")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP 4 — IV Velocity  (NaN when IV columns absent from morning_df)
    # ═══════════════════════════════════════════════════════════════════════════
    atm_ce_iv = col(_ATM_CE_IV)
    atm_pe_iv = col(_ATM_PE_IV)
    iv_skew_series = col(_IV_SKEW)

    ce_iv_open = _first(atm_ce_iv)
    pe_iv_open = _first(atm_pe_iv)
    ce_iv_mid = mid(_ATM_CE_IV)
    pe_iv_mid = mid(_ATM_PE_IV)
    iv_skew_open = _first(iv_skew_series)
    iv_skew_mid = mid(_IV_SKEW)

    out["vel_atm_ce_iv_delta_open"] = _delta(ce_iv_open, ce_iv_mid)
    out["vel_atm_pe_iv_delta_open"] = _delta(pe_iv_open, pe_iv_mid)
    out["vel_iv_skew_delta_open"] = _delta(iv_skew_open, iv_skew_mid)

    delta_ce_iv = out["vel_atm_ce_iv_delta_open"]
    out["vel_iv_compression_rate"] = delta_ce_iv / minutes_elapsed if math.isfinite(delta_ce_iv) else float("nan")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP 5 — Volume Velocity
    # ═══════════════════════════════════════════════════════════════════════════
    ce_vol = col(_CE_VOL)
    pe_vol = col(_PE_VOL)

    ce_vol_30m_ago = _at_offset(ce_vol, 2)
    pe_vol_30m_ago = _at_offset(pe_vol, 2)
    ce_vol_mid = mid(_CE_VOL)
    pe_vol_mid = mid(_PE_VOL)

    out["vel_ce_vol_delta_30m"] = _delta(ce_vol_30m_ago, ce_vol_mid)
    out["vel_pe_vol_delta_30m"] = _delta(pe_vol_30m_ago, pe_vol_mid)

    # volume acceleration: last 30m vs prev 30m (ratio)
    ce_vol_prev_30m_start = _at_offset(ce_vol, 3)
    pe_vol_prev_30m_start = _at_offset(pe_vol, 3)

    def _vol_accel(start: Optional[float], mid_ago: Optional[float], mid_now: Optional[float]) -> float:
        if start is None or mid_ago is None or mid_now is None:
            return float("nan")
        last_30m = mid_now - mid_ago
        prev_30m = mid_ago - start
        if prev_30m == 0.0:
            return float("nan")
        ratio = last_30m / abs(prev_30m)
        return float(ratio) if math.isfinite(ratio) else float("nan")

    out["vel_options_vol_acceleration"] = _vol_accel(
        ce_vol_prev_30m_start, ce_vol_30m_ago, ce_vol_mid
    )
    current_midday_option_volume = mid("opt_flow_options_volume_total")
    if current_midday_option_volume is None:
        if ce_vol_mid is not None and pe_vol_mid is not None:
            current_midday_option_volume = ce_vol_mid + pe_vol_mid

    if (
        current_midday_option_volume is not None
        and prev_day_midday_option_volume is not None
        and math.isfinite(current_midday_option_volume)
        and math.isfinite(prev_day_midday_option_volume)
        and prev_day_midday_option_volume > 0.0
    ):
        out["ctx_am_vol_vs_yday"] = float(current_midday_option_volume / prev_day_midday_option_volume)
    else:
        out["ctx_am_vol_vs_yday"] = float("nan")

    if (
        current_midday_option_volume is not None
        and avg_20d_midday_option_volume is not None
        and math.isfinite(current_midday_option_volume)
        and math.isfinite(avg_20d_midday_option_volume)
        and avg_20d_midday_option_volume > 0.0
    ):
        out["vol_spike_ratio"] = float(current_midday_option_volume / avg_20d_midday_option_volume)
    else:
        out["vol_spike_ratio"] = float("nan")

    # ADX(14) over the morning window using available bars with Wilder smoothing.
    highs = fut_high.astype(float)
    lows = fut_low.astype(float)
    closes = fut_close.astype(float)
    prev_close_series = closes.shift(1)
    up_move = highs.diff()
    down_move = -lows.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0).fillna(0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0).fillna(0.0)
    tr_components = pd.concat(
        [
            (highs - lows).abs(),
            (highs - prev_close_series).abs(),
            (lows - prev_close_series).abs(),
        ],
        axis=1,
    )
    true_range = tr_components.max(axis=1).fillna(0.0)
    atr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean() / atr.replace(0.0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean() / atr.replace(0.0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan)
    adx = dx.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean()
    last_adx = pd.to_numeric(adx, errors="coerce").dropna()
    out["adx_14"] = float(last_adx.iloc[-1]) if len(last_adx) > 0 else float("nan")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUP 6 — Morning Session Summary
    # ═══════════════════════════════════════════════════════════════════════════

    # ctx_am_trend: dominant trend 10:00–11:30 based on price slope
    price_slope = _linear_slope_normalised(fut_close.dropna())
    out["ctx_am_trend"] = float(_sign_int(price_slope, threshold=_TREND_FLAT_SLOPE_ABS))
    out["ctx_am_trend_strength"] = float(abs(price_slope)) if price_slope is not None else float("nan")

    # ctx_am_reversal: did price change direction in the last 30 min?
    # Compare sign of last-30m move vs sign of open-to-60m-ago move
    price_mid_val = price_mid
    price_30m_val = price_30m_ago
    price_60m_val = price_60m_ago
    if price_mid_val is not None and price_30m_val is not None and price_60m_val is not None:
        prior_move = price_30m_val - price_60m_val
        recent_move = price_mid_val - price_30m_val
        prior_sign = _sign_int(prior_move, threshold=1.0)
        recent_sign = _sign_int(recent_move, threshold=1.0)
        reversal = 1 if (prior_sign != 0 and recent_sign != 0 and prior_sign != recent_sign) else 0
    else:
        reversal = 0
    out["ctx_am_reversal"] = float(reversal)

    # ctx_am_oi_direction: net OI delta direction
    ce_delta_open = out["vel_ce_oi_delta_open"]
    pe_delta_open = out["vel_pe_oi_delta_open"]
    if math.isfinite(ce_delta_open) and math.isfinite(pe_delta_open):
        if ce_delta_open > abs(pe_delta_open) * 0.3:
            out["ctx_am_oi_direction"] = 1.0   # CE building dominates
        elif pe_delta_open > abs(ce_delta_open) * 0.3:
            out["ctx_am_oi_direction"] = -1.0  # PE building dominates
        else:
            out["ctx_am_oi_direction"] = 0.0
    else:
        out["ctx_am_oi_direction"] = float("nan")

    # ctx_am_vwap_side: is 11:30 price above or below VWAP?
    vwap_mid = mid(_VWAP)
    if price_mid is not None and vwap_mid is not None:
        out["ctx_am_vwap_side"] = float(1 if price_mid > vwap_mid else (-1 if price_mid < vwap_mid else 0))
    else:
        out["ctx_am_vwap_side"] = float("nan")

    # ctx_am_breakout_confirmed: did the opening-range breakout hold to 11:30?
    breakout_up = mid("ctx_opening_range_breakout_up")
    breakout_down = mid("ctx_opening_range_breakout_down")
    if breakout_up is not None and breakout_down is not None:
        confirmed = 1 if (breakout_up == 1.0 or breakout_down == 1.0) else 0
    else:
        confirmed = 0
    out["ctx_am_breakout_confirmed"] = float(confirmed)

    # ensure all expected keys are present (fill any missing with NaN)
    for k in _ALL_OUTPUT_COLUMNS:
        if k not in out:
            out[k] = float("nan")

    return out


# ── canonical ordered list of all output columns ───────────────────────────────
_ALL_OUTPUT_COLUMNS: list[str] = [
    # Group 1 — OI Velocity
    "vel_ce_oi_delta_open",
    "vel_pe_oi_delta_open",
    "vel_ce_oi_delta_30m",
    "vel_pe_oi_delta_30m",
    "vel_oi_ratio_delta_open",
    "vel_oi_ratio_delta_30m",
    "vel_ce_oi_build_rate",
    "vel_pe_oi_build_rate",
    # Group 2 — PCR Velocity
    "vel_pcr_delta_open",
    "vel_pcr_delta_30m",
    "vel_pcr_acceleration",
    "vel_pcr_trend_direction",
    # Group 3 — Price Velocity
    "vel_price_delta_open",
    "vel_price_delta_30m",
    "vel_price_delta_60m",
    "vel_price_acceleration",
    "ctx_am_range_high",
    "ctx_am_range_low",
    "ctx_am_range_size",
    "ctx_am_price_position",
    "ctx_am_gap_from_yday",
    "ctx_am_gap_filled",
    # Group 4 — IV Velocity
    "vel_atm_ce_iv_delta_open",
    "vel_atm_pe_iv_delta_open",
    "vel_iv_skew_delta_open",
    "vel_iv_compression_rate",
    # Group 5 — Volume Velocity
    "vel_ce_vol_delta_30m",
    "vel_pe_vol_delta_30m",
    "vel_options_vol_acceleration",
    "ctx_am_vol_vs_yday",
    "adx_14",
    "vol_spike_ratio",
    # Group 6 — Morning Session Summary
    "ctx_am_trend",
    "ctx_am_trend_strength",
    "ctx_am_reversal",
    "ctx_am_oi_direction",
    "ctx_am_vwap_side",
    "ctx_am_breakout_confirmed",
    "ctx_gap_pct",
    "ctx_gap_up",
    "ctx_gap_down",
]

# columns that are integer-typed (-1/0/1 or 0/1) in the contract
VELOCITY_INTEGER_COLUMNS: frozenset[str] = frozenset({
    "vel_pcr_trend_direction",
    "ctx_am_gap_filled",
    "ctx_gap_up",
    "ctx_gap_down",
    "ctx_am_trend",
    "ctx_am_reversal",
    "ctx_am_oi_direction",
    "ctx_am_vwap_side",
    "ctx_am_breakout_confirmed",
})

VELOCITY_COLUMNS: list[str] = list(_ALL_OUTPUT_COLUMNS)

__all__ = [
    "compute_velocity_features",
    "VELOCITY_COLUMNS",
    "VELOCITY_INTEGER_COLUMNS",
]
