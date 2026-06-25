"""
Layered feature engine — single transformation pipeline for training AND runtime.

Every derived feature is computed ONCE, in a fixed order, from the same code.
This eliminates train/serve skew by definition: there is only one path.

Architecture
────────────
  Raw bars (px_fut_open/high/low/close, OI, IV, volume)
    → Layer 0: schema normalisation   aliases → canonical v2 column names
    → Layer 1: returns                ret_1m / ret_3m / ret_5m / ret_10m / ret_15m / ret_30m / ret_open
    → Layer 2: technicals             ema_9/21/50, rsi_14, atr_14, adx_14, bollinger, momentum, vol_ratio
    → Layer 3: session context        vwap, orb, day_high/low, time features
    → Layer 4: velocity               vel_* / ctx_am_* / ctx_gap_*  (compute_per_bar_velocity_df)
    → Layer 5: compression            comp_* / compression_score    (add_compression_features_from_flat)
    → Layer 6: derived context        ctx_dte_*, ctx_is_*, ctx_regime_*

Usage
─────
  # Training (dhan_data_pipeline._build_day_indicators)
  df = build_features(bars_df, trade_date=td, prev_day_close=prev_close, vix_open=vix)

  # Runtime (live_feature_engine.LiveFeatureAccumulator.snapshot())
  df = build_features(accumulated_bars, trade_date=today)

Both calls receive the same df shape and produce the same column set.
Columns from earlier layers are never overwritten by later layers.
Missing input columns degrade the relevant output to NaN — never crash.

Input contract
──────────────
df:
  - DatetimeIndex (IST tz-aware) or timestamp column, 1-min bars
  - Single trade_date, sorted ascending (earliest first)
  - Core OHLC:      px_fut_open / px_fut_high / px_fut_low / px_fut_close
                    (or plain open/high/low/close — Layer 0 normalises them)
  - Options flow:   opt_flow_ce_oi_total / opt_flow_pe_oi_total / opt_flow_pcr_oi
                    atm_oi_ratio / atm_ce_iv / atm_pe_iv / iv_skew
                    opt_flow_ce_volume_total / opt_flow_pe_volume_total
  - Volume:         fut_flow_volume  (or volume)
  - VIX:            vix  (optional column — or pass vix_open kwarg)

All optional: missing columns → NaN for features that need them.
"""

from __future__ import annotations

import math
import logging
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from snapshot_app.core.velocity_features import compute_per_bar_velocity_df, VELOCITY_COLUMNS
from snapshot_app.core.compression_features import add_compression_features_from_flat

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Column registry — canonical names for every feature, grouped by layer.
# Use this as the authoritative reference: add new features here first.
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA: Dict[str, List[str]] = {
    "raw": [
        "px_fut_open", "px_fut_high", "px_fut_low", "px_fut_close",
        "px_spot_open", "px_spot_high", "px_spot_low", "px_spot_close",
        "fut_flow_volume", "fut_flow_oi",
        "opt_flow_ce_oi_total", "opt_flow_pe_oi_total", "opt_flow_pcr_oi",
        "opt_flow_ce_volume_total", "opt_flow_pe_volume_total",
        "atm_oi_ratio", "atm_ce_iv", "atm_pe_iv", "iv_skew",
        "vix",
    ],
    "returns": [
        "ret_1m", "ret_3m", "ret_5m", "ret_10m", "ret_15m", "ret_30m", "ret_open",
    ],
    "technicals": [
        "ema_9", "ema_21", "ema_50",
        "ema_9_21_spread", "ema_above_21",
        "ema_9_slope", "ema_21_slope", "ema_50_slope",
        "osc_rsi_14",
        "osc_atr_14", "osc_atr_ratio", "osc_atr_percentile", "osc_atr_daily_percentile",
        "adx_14",
        "bb_upper", "bb_lower", "bb_width", "bb_position",
        "realized_vol_5m", "realized_vol_15m", "realized_vol_30m",
        "momentum_5m", "momentum_15m",
        "vol_spike_ratio",
        "fut_flow_oi_change_1m", "fut_flow_oi_change_5m",
        "pcr_change_5m", "pcr_change_15m", "pcr_change_30m",
    ],
    "flow": [
        "dist_basis", "dist_basis_change_1m",
        "fut_flow_rel_volume_20", "fut_flow_volume_accel_1m",
        "fut_flow_oi_rel_20", "fut_flow_oi_zscore_20",
        "opt_flow_ce_pe_oi_diff", "opt_flow_ce_pe_volume_diff",
        "opt_flow_options_volume_total", "opt_flow_rel_volume_20",
        "opt_flow_atm_strike", "opt_flow_rows",
        "opt_flow_atm_call_return_1m", "opt_flow_atm_put_return_1m",
        "opt_flow_atm_oi_change_1m",
        "atm_oi_ratio", "near_atm_oi_ratio",
    ],
    "session": [
        "vwap_fut", "vwap_distance", "ctx_above_vwap",
        "day_high", "day_low", "dist_from_day_high", "dist_from_day_low",
        "ctx_opening_range_high", "ctx_opening_range_low",
        "ctx_opening_range_width", "ctx_orb_width_pct",
        "ctx_opening_range_ready",
        "ctx_opening_range_breakout_up", "ctx_opening_range_breakout_down",
        "orb_high_reject", "orb_low_reject",
        "time_minute_index", "time_minute_of_day", "time_day_of_week",
        "minutes_to_close",
    ],
    "velocity": list(VELOCITY_COLUMNS),
    # compression columns sourced at import-time from compression_features
    "compression": [],   # filled below after lazy import
    "context": [
        "ctx_dte_days", "ctx_is_expiry_day", "ctx_is_near_expiry",
        "ctx_is_high_vix_day",
        "vix_open_day", "vix_intraday_chg",
        "ctx_regime_trend_up", "ctx_regime_trend_down",
        "ctx_regime_atr_high", "ctx_regime_atr_low",
        "ctx_regime_vol_high", "ctx_regime_expiry_near",
    ],
}

# Lazily populate compression columns from the source module
try:
    from snapshot_app.core.compression_features import COMPRESSION_FEATURE_COLUMNS
    SCHEMA["compression"] = list(COMPRESSION_FEATURE_COLUMNS)
except ImportError:
    pass

ALL_FEATURE_COLUMNS: List[str] = [
    col for cols in SCHEMA.values() for col in cols
]

# ══════════════════════════════════════════════════════════════════════════════
# Primitive helpers (single source — do not duplicate in pipeline scripts)
# ══════════════════════════════════════════════════════════════════════════════

_SESSION_START  = "09:15"
_SESSION_END    = "15:30"
_ORB_BARS       = 15          # first 15 min = opening range
_VIX_HIGH_THRESHOLD = 18.0

try:
    from datetime import timezone, timedelta as _td
    IST = timezone(_td(hours=5, minutes=30))
except Exception:
    IST = None  # type: ignore[assignment]


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=1).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0.0)
    loss     = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()
    rs       = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    up_move    = high.diff()
    dn_move    = -low.diff()
    plus_dm  = up_move.where((up_move > dn_move) & (up_move > 0.0), 0.0).fillna(0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0.0), 0.0).fillna(0.0)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1).fillna(0.0)
    alpha    = 1.0 / period
    atr      = tr.ewm(alpha=alpha, adjust=False, min_periods=1).mean()
    safe_atr = atr.replace(0.0, np.nan)
    plus_di  = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=1).mean() / safe_atr
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=1).mean() / safe_atr
    denom    = (plus_di + minus_di).replace(0.0, np.nan)
    dx       = 100.0 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=alpha, adjust=False, min_periods=1).mean()


def _next_weekly_expiry(trade_date: date) -> date:
    target_wd = 2 if trade_date.year >= 2024 else 3   # Wed=2, Thu=3
    days_ahead = (target_wd - trade_date.weekday()) % 7
    return trade_date + timedelta(days=days_ahead)


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Return column as float series; NaN-filled if absent."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype=float)


def _resolve(df: pd.DataFrame, names: Sequence[str]) -> pd.Series:
    """First present column among `names` as float series (NaN if none present).

    This is what lets one function serve both naming conventions:
    training assembly (atm_ce_oi, opt_flow_ce_oi_total) and the live panel
    (opt_0_ce_oi, ce_oi_total). Add an alias here, not a second code path.
    """
    for name in names:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype=float)


def _resolve_sum(df: pd.DataFrame, names: Sequence[str]) -> pd.Series:
    """Sum of all present columns among `names` (NaN if none present, min_count=1)."""
    present = [pd.to_numeric(df[n], errors="coerce") for n in names if n in df.columns]
    if not present:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return pd.concat(present, axis=1).sum(axis=1, min_count=1)


def _rel_with_zero_guard(value: pd.Series, window: int = 20, min_p: int = 5) -> pd.Series:
    """value / rolling-mean(value); a window of all-zeros maps to 0.0 (not NaN).

    Mirrors runtime_features._add_group_features zero-window handling so the
    rel-volume / rel-OI features match across training and live exactly.
    """
    roll = value.rolling(window, min_periods=min_p).mean()
    out = value / roll.replace(0.0, np.nan)
    zero_win = value.fillna(0.0).eq(0.0) & roll.fillna(0.0).eq(0.0)
    out = out.where(~zero_win, 0.0)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Layer 0 — Schema normalisation
# Maps raw/alias column names to canonical v2 schema names.
# Never overwrites a column that already exists.
# ══════════════════════════════════════════════════════════════════════════════

_L0_ALIASES: Dict[str, str] = {
    # plain OHLCV → px_fut_* / fut_flow_*
    "open":          "px_fut_open",
    "high":          "px_fut_high",
    "low":           "px_fut_low",
    "close":         "px_fut_close",
    "volume":        "fut_flow_volume",
    "oi":            "fut_flow_oi",
    # live-panel futures names (live_ml_flat _PANEL_SOURCE_COLUMNS)
    "fut_close":     "px_fut_close",
    "fut_high":      "px_fut_high",
    "fut_low":       "px_fut_low",
    "fut_open":      "px_fut_open",
    "fut_volume":    "fut_flow_volume",
    "fut_oi":        "fut_flow_oi",
    "spot_close":    "px_spot_close",
    "spot_high":     "px_spot_high",
    "spot_low":      "px_spot_low",
    "spot_open":     "px_spot_open",
    # option flow aliases (training + live-panel)
    "pcr":           "opt_flow_pcr_oi",
    "pcr_oi":        "opt_flow_pcr_oi",
    "total_ce_oi":   "opt_flow_ce_oi_total",
    "total_pe_oi":   "opt_flow_pe_oi_total",
    "ce_oi_total":   "opt_flow_ce_oi_total",
    "pe_oi_total":   "opt_flow_pe_oi_total",
    "ce_volume":     "opt_flow_ce_volume_total",
    "pe_volume":     "opt_flow_pe_volume_total",
    "ce_volume_total": "opt_flow_ce_volume_total",
    "pe_volume_total": "opt_flow_pe_volume_total",
}


def _layer_0_normalise(df: pd.DataFrame) -> pd.DataFrame:
    for alias, canonical in _L0_ALIASES.items():
        if alias in df.columns and canonical not in df.columns:
            df[canonical] = df[alias]
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — Returns
# ══════════════════════════════════════════════════════════════════════════════

def _layer_1_returns(df: pd.DataFrame) -> pd.DataFrame:
    sc = _col(df, "px_fut_close")
    open0 = float(sc.iloc[0]) if pd.notna(sc.iloc[0]) else float("nan")
    denom_open = open0 if open0 != 0.0 else float("nan")

    for periods, name in [(1, "ret_1m"), (3, "ret_3m"), (5, "ret_5m"),
                          (10, "ret_10m"), (15, "ret_15m"), (30, "ret_30m")]:
        if name not in df.columns:
            df[name] = sc.pct_change(periods, fill_method=None)

    if "ret_open" not in df.columns:
        df["ret_open"] = (sc - open0) / denom_open

    return df


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2 — Technical indicators
# All computed from px_fut_open/high/low/close and fut_flow_volume.
# ══════════════════════════════════════════════════════════════════════════════

def _layer_2_technicals(df: pd.DataFrame) -> pd.DataFrame:
    sc  = _col(df, "px_fut_close")
    shi = _col(df, "px_fut_high")
    slo = _col(df, "px_fut_low")
    vol = _col(df, "fut_flow_volume").clip(lower=0.0)
    pcr = _col(df, "opt_flow_pcr_oi")
    foi = _col(df, "fut_flow_oi")

    # EMAs
    for span, name in [(9, "ema_9"), (21, "ema_21"), (50, "ema_50")]:
        if name not in df.columns:
            df[name] = _ema(sc, span)
    if "ema_9_21_spread" not in df.columns:
        df["ema_9_21_spread"] = (df["ema_9"] - df["ema_21"]) / sc.replace(0.0, np.nan)
    if "ema_above_21" not in df.columns:
        df["ema_above_21"] = (df["ema_9"] > df["ema_21"]).astype(int)
    if "ema_9_slope" not in df.columns:
        df["ema_9_slope"] = df["ema_9"].diff()
    if "ema_21_slope" not in df.columns:
        df["ema_21_slope"] = df["ema_21"].diff()
    if "ema_50_slope" not in df.columns:
        df["ema_50_slope"] = df["ema_50"].diff()

    # RSI / ATR / ADX
    if "osc_rsi_14" not in df.columns:
        df["osc_rsi_14"] = _rsi(sc, 14)
    if "osc_atr_14" not in df.columns:
        df["osc_atr_14"] = _atr(shi, slo, sc, 14)
    if "osc_atr_ratio" not in df.columns:
        df["osc_atr_ratio"] = df["osc_atr_14"] / sc.replace(0.0, np.nan)
    if "osc_atr_percentile" not in df.columns:
        # Intraday expanding percentile rank of atr_ratio (single-day causal).
        df["osc_atr_percentile"] = df["osc_atr_ratio"].expanding(min_periods=20).rank(pct=True)
    if "osc_atr_daily_percentile" not in df.columns:
        # Cross-session feature — inherently needs prior-day history.
        # Single-day build leaves NaN; the cross-day assemble step fills it.
        df["osc_atr_daily_percentile"] = np.nan
    if "adx_14" not in df.columns:
        df["adx_14"] = _adx(shi, slo, sc, 14)

    # Bollinger Bands (20-bar)
    if "bb_width" not in df.columns:
        bb_mid = sc.rolling(20, min_periods=5).mean()
        bb_std = sc.rolling(20, min_periods=5).std()
        bb_upper = bb_mid + 2.0 * bb_std
        bb_lower = bb_mid - 2.0 * bb_std
        bb_range = (bb_upper - bb_lower).replace(0.0, np.nan)
        df["bb_upper"]    = bb_upper
        df["bb_lower"]    = bb_lower
        df["bb_width"]    = bb_range / bb_mid.replace(0.0, np.nan)
        df["bb_position"] = (sc - bb_lower) / bb_range

    # Realized volatility (annualized, 375 bars/day)
    log_ret = np.log(sc / sc.shift(1))
    for window, name in [(5, "realized_vol_5m"), (15, "realized_vol_15m"), (30, "realized_vol_30m")]:
        if name not in df.columns:
            df[name] = log_ret.rolling(window, min_periods=window).std() * np.sqrt(375.0)

    # Momentum (price relative to rolling mean)
    for window, name in [(5, "momentum_5m"), (15, "momentum_15m")]:
        if name not in df.columns:
            df[name] = (sc - sc.rolling(window, min_periods=3).mean()) / sc.replace(0.0, np.nan)

    # Volume spike ratio (vs 20-bar rolling mean)
    if "vol_spike_ratio" not in df.columns:
        vol_ma = vol.rolling(20, min_periods=5).mean().replace(0.0, np.nan)
        df["vol_spike_ratio"] = vol / vol_ma

    # Futures OI change (1m and 5m)
    if "fut_flow_oi_change_1m" not in df.columns:
        df["fut_flow_oi_change_1m"] = foi.diff(1)
    if "fut_flow_oi_change_5m" not in df.columns:
        df["fut_flow_oi_change_5m"] = foi.diff(5)

    # PCR momentum (contract names pcr_change_5m/15m; 30m kept as extra)
    if "pcr_change_5m" not in df.columns:
        df["pcr_change_5m"] = pcr.diff(5)
    if "pcr_change_15m" not in df.columns:
        df["pcr_change_15m"] = pcr.diff(15)
    if "pcr_change_30m" not in df.columns:
        df["pcr_change_30m"] = pcr.diff(30)

    return df


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2b — Futures flow, basis, option flow derivatives
# All contract opt_flow_* / fut_flow_* / dist_basis columns.
# Alias-resolved inputs → one function serves training + live naming.
# ══════════════════════════════════════════════════════════════════════════════

def _layer_2b_flow(df: pd.DataFrame) -> pd.DataFrame:
    sc   = _col(df, "px_fut_close")
    vol  = _col(df, "fut_flow_volume")
    foi  = _col(df, "fut_flow_oi")
    spot = _resolve(df, ["px_spot_close", "spot_close"])

    # ── Basis (fut − spot) → contract dist_basis ─────────────────────────
    if "dist_basis" not in df.columns:
        basis = sc - spot
        df["dist_basis"] = basis
        df["dist_basis_change_1m"] = basis.diff(1)

    # ── Futures flow: rel-volume, accel, rel-OI, OI z-score ──────────────
    if "fut_flow_rel_volume_20" not in df.columns:
        df["fut_flow_rel_volume_20"] = _rel_with_zero_guard(vol, 20, 5)
    if "fut_flow_volume_accel_1m" not in df.columns:
        df["fut_flow_volume_accel_1m"] = vol.pct_change(1, fill_method=None).replace([np.inf, -np.inf], np.nan)
    if "fut_flow_oi_rel_20" not in df.columns:
        df["fut_flow_oi_rel_20"] = _rel_with_zero_guard(foi, 20, 5)
    if "fut_flow_oi_zscore_20" not in df.columns:
        oi_roll = foi.rolling(20, min_periods=5).mean()
        oi_std_raw = foi.rolling(20, min_periods=5).std(ddof=0)
        z = (foi - oi_roll) / oi_std_raw.replace(0.0, np.nan)
        zero_win = oi_std_raw.fillna(0.0).eq(0.0) & (foi - oi_roll).fillna(0.0).eq(0.0)
        df["fut_flow_oi_zscore_20"] = z.where(~zero_win, 0.0)

    # ── Option flow aggregates (alias-resolved CE/PE totals) ─────────────
    ce_oi  = _resolve(df, ["opt_flow_ce_oi_total", "ce_oi_total"])
    pe_oi  = _resolve(df, ["opt_flow_pe_oi_total", "pe_oi_total"])
    ce_vol = _resolve(df, ["opt_flow_ce_volume_total", "ce_volume_total"])
    pe_vol = _resolve(df, ["opt_flow_pe_volume_total", "pe_volume_total"])
    pcr    = _resolve(df, ["opt_flow_pcr_oi", "pcr_oi"])

    if "opt_flow_ce_pe_oi_diff" not in df.columns:
        df["opt_flow_ce_pe_oi_diff"] = ce_oi - pe_oi
    if "opt_flow_ce_pe_volume_diff" not in df.columns:
        df["opt_flow_ce_pe_volume_diff"] = ce_vol - pe_vol
    if "opt_flow_options_volume_total" not in df.columns:
        df["opt_flow_options_volume_total"] = ce_vol + pe_vol
    if "opt_flow_rel_volume_20" not in df.columns:
        df["opt_flow_rel_volume_20"] = _rel_with_zero_guard(ce_vol + pe_vol, 20, 5)

    # ── ATM strike + same-ATM-guarded returns / OI change ────────────────
    atm_strike = _resolve(df, ["opt_flow_atm_strike", "atm_strike"])
    if "opt_flow_atm_strike" not in df.columns:
        df["opt_flow_atm_strike"] = atm_strike
    same_atm = atm_strike.notna() & atm_strike.eq(atm_strike.shift(1))

    atm_ce_close = _resolve(df, ["opt_0_ce_close", "atm_ce_ltp", "atm_ce_close"])
    atm_pe_close = _resolve(df, ["opt_0_pe_close", "atm_pe_ltp", "atm_pe_close"])
    atm_ce_oi    = _resolve(df, ["atm_ce_oi", "opt_0_ce_oi"])
    atm_pe_oi    = _resolve(df, ["atm_pe_oi", "opt_0_pe_oi"])

    if "opt_flow_atm_call_return_1m" not in df.columns:
        df["opt_flow_atm_call_return_1m"] = atm_ce_close.pct_change(1, fill_method=None).where(same_atm)
    if "opt_flow_atm_put_return_1m" not in df.columns:
        df["opt_flow_atm_put_return_1m"] = atm_pe_close.pct_change(1, fill_method=None).where(same_atm)
    if "opt_flow_atm_oi_change_1m" not in df.columns:
        df["opt_flow_atm_oi_change_1m"] = (atm_ce_oi + atm_pe_oi).diff(1).where(same_atm)

    # ── atm_oi_ratio + near_atm_oi_ratio (±1 strike) ─────────────────────
    if "atm_oi_ratio" not in df.columns:
        atm_total = (atm_ce_oi + atm_pe_oi).replace(0.0, np.nan)
        df["atm_oi_ratio"] = (atm_ce_oi / atm_total).where(atm_ce_oi.notna() & atm_pe_oi.notna())
    if "near_atm_oi_ratio" not in df.columns:
        near_ce = _resolve_sum(df, ["opt_m1_ce_oi", "opt_0_ce_oi", "opt_p1_ce_oi"])
        near_pe = _resolve_sum(df, ["opt_m1_pe_oi", "opt_0_pe_oi", "opt_p1_pe_oi"])
        near_total = (near_ce + near_pe).replace(0.0, np.nan)
        near_ratio = (near_ce / near_total).where(near_ce.notna() & near_pe.notna())
        # fall back to atm_oi_ratio when near strikes unavailable
        df["near_atm_oi_ratio"] = near_ratio.where(near_ratio.notna(), df.get("atm_oi_ratio"))

    # ── opt_flow_rows (chain breadth) — pass through if provided ─────────
    if "opt_flow_rows" not in df.columns:
        df["opt_flow_rows"] = _resolve(df, ["opt_flow_rows", "options_rows"])

    # ── ATM IV + skew (training-truth definition: raw ce_iv − pe_iv) ─────
    ce_iv = _resolve(df, ["atm_ce_iv", "opt_0_ce_iv"])
    pe_iv = _resolve(df, ["atm_pe_iv", "opt_0_pe_iv"])
    if "atm_iv" not in df.columns:
        df["atm_iv"] = (ce_iv + pe_iv) / 2.0
    if "iv_skew" not in df.columns:
        df["iv_skew"] = ce_iv - pe_iv

    return df


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3 — Session context
# VWAP, opening range, day range, time features.
# ══════════════════════════════════════════════════════════════════════════════

def _layer_3_session(df: pd.DataFrame, *, n_total_bars: int = 376) -> pd.DataFrame:
    sc  = _col(df, "px_fut_close")
    shi = _col(df, "px_fut_high")
    slo = _col(df, "px_fut_low")
    sop = _col(df, "px_fut_open")
    vol = _col(df, "fut_flow_volume").clip(lower=0.0)

    # ── VWAP (typical price × volume, cumulative from bar 0) ─────────────
    if "vwap_fut" not in df.columns:
        tp          = (shi + slo + sc) / 3.0
        valid       = tp.notna() & (vol > 0.0)
        cum_vol     = vol.where(valid, 0.0).cumsum()
        cum_pv      = (tp.where(valid, 0.0) * vol.where(valid, 0.0)).cumsum()
        vwap        = pd.Series(np.nan, index=df.index)
        pos         = cum_vol > 0.0
        vwap[pos]   = cum_pv[pos] / cum_vol[pos]
        df["vwap_fut"]      = vwap
        df["vwap_distance"] = (sc - vwap) / vwap.replace(0.0, np.nan)
        df["ctx_above_vwap"]= (sc > vwap).astype(int)

    # ── Day range (expanding from open) ──────────────────────────────────
    if "day_high" not in df.columns:
        df["day_high"] = shi.expanding().max()
        df["day_low"]  = slo.expanding().min()
        df["dist_from_day_high"] = (df["day_high"] - sc) / sc.replace(0.0, np.nan)
        df["dist_from_day_low"]  = (sc - df["day_low"])  / sc.replace(0.0, np.nan)

    # ── Opening range (first _ORB_BARS bars = 15 min) ────────────────────
    if "ctx_opening_range_high" not in df.columns:
        orb_slice = df.iloc[:_ORB_BARS]
        orb_high  = float(_col(orb_slice, "px_fut_high").max())
        orb_low   = float(_col(orb_slice, "px_fut_low").min())
        orb_open  = float(sop.iloc[0]) if pd.notna(sop.iloc[0]) else float("nan")
        orb_width = orb_high - orb_low
        df["ctx_opening_range_high"]          = orb_high
        df["ctx_opening_range_low"]           = orb_low
        df["ctx_opening_range_width"]         = orb_width
        df["ctx_orb_width_pct"]               = orb_width / orb_open * 100.0 if orb_open != 0.0 else np.nan
        df["ctx_opening_range_breakout_up"]   = (sc > orb_high).astype(int)
        df["ctx_opening_range_breakout_down"] = (sc < orb_low).astype(int)
        df["orb_high_reject"]                 = ((sc.shift(1) > orb_high) & (sc < orb_high)).astype(int)
        df["orb_low_reject"]                  = ((sc.shift(1) < orb_low)  & (sc > orb_low)).astype(int)

    # ── Opening-range ready flag (>= _ORB_BARS bars elapsed) ─────────────
    if "ctx_opening_range_ready" not in df.columns:
        bar_pos = np.arange(len(df))
        df["ctx_opening_range_ready"] = (bar_pos >= _ORB_BARS).astype(int)

    # ── Time features ─────────────────────────────────────────────────────
    # bar index (0 at 9:15) is always positional; minute_of_day/day_of_week
    # come from the DatetimeIndex when present so training and live agree.
    n = len(df)
    if "time_minute_index" not in df.columns:
        df["time_minute_index"] = np.arange(n)
    if "minutes_to_close" not in df.columns:
        df["minutes_to_close"] = max(0, n_total_bars - 1) - pd.Series(np.arange(n), index=df.index)
    is_dt_index = isinstance(df.index, pd.DatetimeIndex)
    if "time_minute_of_day" not in df.columns:
        if is_dt_index:
            df["time_minute_of_day"] = df.index.hour * 60 + df.index.minute
        else:
            df["time_minute_of_day"] = np.arange(n)
    if "time_day_of_week" not in df.columns:
        df["time_day_of_week"] = df.index.dayofweek if is_dt_index else np.nan

    return df


# ══════════════════════════════════════════════════════════════════════════════
# Layer 4 — Velocity
# Delegates entirely to compute_per_bar_velocity_df (velocity_features.py).
# ══════════════════════════════════════════════════════════════════════════════

def _layer_4_velocity(
    df: pd.DataFrame,
    *,
    prev_day_close: Optional[float] = None,
    prev_day_midday_option_volume: Optional[float] = None,
    avg_20d_midday_option_volume: Optional[float] = None,
) -> pd.DataFrame:
    return compute_per_bar_velocity_df(
        df,
        prev_day_close=prev_day_close,
        prev_day_midday_option_volume=prev_day_midday_option_volume,
        avg_20d_midday_option_volume=avg_20d_midday_option_volume,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Layer 5 — Compression
# Delegates entirely to add_compression_features_from_flat (compression_features.py).
# ══════════════════════════════════════════════════════════════════════════════

def _layer_5_compression(df: pd.DataFrame) -> pd.DataFrame:
    return add_compression_features_from_flat(df)


# ══════════════════════════════════════════════════════════════════════════════
# Layer 6 — Derived context
# DTE, VIX regime, EMA regime. Depends on L2 technicals.
# ══════════════════════════════════════════════════════════════════════════════

def _layer_6_context(
    df: pd.DataFrame,
    *,
    trade_date: Optional[date] = None,
    vix_open: Optional[float] = None,
    expiry_date: Optional[date] = None,
) -> pd.DataFrame:
    # ── Expiry / DTE ──────────────────────────────────────────────────────
    if "ctx_dte_days" not in df.columns:
        td = trade_date
        if td is None:
            # infer from DatetimeIndex
            try:
                td = df.index[0].date()
            except Exception:
                td = None
        if td is not None:
            exp = expiry_date if expiry_date is not None else _next_weekly_expiry(td)
            dte = (exp - td).days
            df["ctx_dte_days"]           = dte
            df["ctx_is_expiry_day"]      = int(dte == 0)
            df["ctx_is_near_expiry"]     = int(dte <= 1)
            df["ctx_regime_expiry_near"] = int(dte <= 1)
        else:
            for c in ["ctx_dte_days", "ctx_is_expiry_day", "ctx_is_near_expiry",
                      "ctx_regime_expiry_near"]:
                df[c] = np.nan

    # ── VIX regime ────────────────────────────────────────────────────────
    if "ctx_is_high_vix_day" not in df.columns:
        # prefer vix_open kwarg; fall back to vix column first bar
        v = vix_open
        if v is None and "vix" in df.columns:
            vix_s = _col(df, "vix")
            first_valid = vix_s.dropna()
            v = float(first_valid.iloc[0]) if len(first_valid) > 0 else None
        if v is not None and math.isfinite(float(v)):
            df["ctx_is_high_vix_day"] = int(float(v) > _VIX_HIGH_THRESHOLD)
            if "vix_open_day" not in df.columns:
                df["vix_open_day"] = float(v)
            if "vix" in df.columns and "vix_intraday_chg" not in df.columns:
                df["vix_intraday_chg"] = (_col(df, "vix") - float(v)) / float(v) * 100.0
        else:
            df["ctx_is_high_vix_day"] = np.nan

    # ── EMA trend regime (depends on L2 ema_9/21/50) ─────────────────────
    if "ctx_regime_trend_up" not in df.columns and "ema_9" in df.columns:
        e9  = _col(df, "ema_9")
        e21 = _col(df, "ema_21")
        e50 = _col(df, "ema_50")
        df["ctx_regime_trend_up"]   = ((e9 > e21) & (e21 > e50)).astype(int)
        df["ctx_regime_trend_down"] = ((e9 < e21) & (e21 < e50)).astype(int)

    # ── ATR regime (vs intraday expanding median — depends on L2 osc_atr_14) ─
    if "ctx_regime_atr_high" not in df.columns and "osc_atr_14" in df.columns:
        atr14 = _col(df, "osc_atr_14")
        atr_med = atr14.expanding(min_periods=5).median()
        df["ctx_regime_atr_high"] = (atr14 > atr_med).astype(int)
        df["ctx_regime_atr_low"]  = (atr14 < atr_med).astype(int)

    # ── Volume spike regime (depends on L2 vol_spike_ratio) ──────────────
    if "ctx_regime_vol_high" not in df.columns and "vol_spike_ratio" in df.columns:
        df["ctx_regime_vol_high"] = (_col(df, "vol_spike_ratio") > 1.5).astype(int)

    return df


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════

ALL_LAYERS: Sequence[str] = ("0_normalise", "1_returns", "2_technicals",
                              "2b_flow", "3_session", "4_velocity",
                              "5_compression", "6_context")


def build_features(
    df: pd.DataFrame,
    *,
    trade_date: Optional[date] = None,
    prev_day_close: Optional[float] = None,
    vix_open: Optional[float] = None,
    expiry_date: Optional[date] = None,
    prev_day_midday_option_volume: Optional[float] = None,
    avg_20d_midday_option_volume: Optional[float] = None,
    n_total_bars: int = 376,
    layers: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """
    Run the full layered feature pipeline on a single-day bar DataFrame.

    Parameters
    ──────────
    df                          : 1-min bars, DatetimeIndex or timestamp col, sorted asc
    trade_date                  : date of the session (inferred from index if None)
    prev_day_close              : prior day's last futures close (for gap features)
    vix_open                    : VIX at session open (for high-vix regime flag)
    expiry_date                 : weekly expiry date (computed if None)
    prev_day_midday_option_volume: for ctx_am_vol_vs_yday velocity feature
    avg_20d_midday_option_volume : for vol_spike_ratio velocity feature
    n_total_bars                : session length for minutes_to_close (default 376 = 09:15-15:30)
    layers                      : subset of ALL_LAYERS to run (default = all)

    Returns
    ───────
    Enriched DataFrame with all feature columns added. Input columns are never overwritten.
    Each column is present exactly once — later layers skip columns that already exist.
    """
    if df is None or len(df) == 0:
        return df

    run = set(layers) if layers is not None else set(ALL_LAYERS)
    df  = df.copy()

    if trade_date is None:
        try:
            trade_date = df.index[0].date()
        except Exception:
            pass

    if "0_normalise"  in run: df = _layer_0_normalise(df)
    if "1_returns"    in run: df = _layer_1_returns(df)
    if "2_technicals" in run: df = _layer_2_technicals(df)
    if "2b_flow"      in run: df = _layer_2b_flow(df)
    if "3_session"    in run: df = _layer_3_session(df, n_total_bars=n_total_bars)
    if "4_velocity"   in run:
        df = _layer_4_velocity(
            df,
            prev_day_close=prev_day_close,
            prev_day_midday_option_volume=prev_day_midday_option_volume,
            avg_20d_midday_option_volume=avg_20d_midday_option_volume,
        )
    if "5_compression" in run: df = _layer_5_compression(df)
    if "6_context"     in run:
        df = _layer_6_context(
            df,
            trade_date=trade_date,
            vix_open=vix_open,
            expiry_date=expiry_date,
        )

    return df


__all__ = [
    "build_features",
    "ALL_LAYERS",
    "SCHEMA",
    "ALL_FEATURE_COLUMNS",
    # helpers — importable for testing individual layers
    "_layer_0_normalise",
    "_layer_1_returns",
    "_layer_2_technicals",
    "_layer_3_session",
    "_layer_4_velocity",
    "_layer_5_compression",
    "_layer_6_context",
    # primitives
    "_ema", "_rsi", "_atr", "_adx", "_next_weekly_expiry",
]
