"""Direction structure features — 5-component weighted direction score.

Framework (OPS-DIRECTION-2026-06-18):
  1. Price structure  (40% → ±2): higher lows vs lower highs over 10 bars
  2. VWAP acceptance  (25% → ±2): 3 consecutive closes above/below VWAP
  3. EMA quality      (15% → ±1): EMA9>21>50 alignment (reuses ema_order from compression_features)
  4. Pressure score   (10% → ±1): bullish/bearish candle bodies over 10 bars (buyers defend dips)
  5. Session position (10% → ±1): price near session high (≥0.85) or low (≤0.15)

Total range: −7 to +7.
  ≥ +5  STRONG LONG
  +3/+4  LONG
  −2..+2 NO DIRECTION (abstain)
  −3/−4  SHORT
  ≤ −5  STRONG SHORT

Parity: same logic for live (`add_direction_features`, uses `close/high/low/vwap/ema_order/
position_in_day_range` columns) and historical flat (`add_direction_features_from_flat`,
uses `px_fut_*` naming). Zero train/serve skew.

DEPENDENCY: `add_compression_features` must run BEFORE `add_direction_features` on the
same frame (for `ema_order` and `position_in_day_range`). The flat variant handles this
internally.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd

from .compression_features import add_compression_features_from_flat

# ── public column contract ───────────────────────────────────────────────────
DIRECTION_FEATURE_COLUMNS: Tuple[str, ...] = (
    "dir_structure",    # ±2 / 0 — price structure: net higher-lows vs lower-highs (10 bars)
    "dir_vwap_hold",    # ±2 / 0 — 3 consecutive closes above (CE) / below (PE) VWAP
    "dir_ema",          # ±1 / 0 — EMA9>21>50 (bull) or EMA9<21<50 (bear) alignment
    "dir_pressure",     # ±1 / 0 — bullish candle bodies dominant (> 60%) or bearish (< 40%)
    "dir_session_pos",  # ±1 / 0 — price in top 15% of day range (+1) or bottom 15% (−1)
    "dir_score",        # −7 to +7 — weighted sum of all five components
)

# Thresholds
_STRUCTURE_N     = 10      # rolling window for price structure
_STRUCTURE_MIN   = 7       # min_periods for the rolling window
_STRUCTURE_BULL  = 0.6     # rising-lows fraction ≥ this → bullish (+2)
_STRUCTURE_BEAR  = 0.6     # falling-highs fraction ≥ this → bearish (−2)
_VWAP_CONSEC_N   = 3       # consecutive closes needed to confirm VWAP hold
_PRESSURE_N      = 10      # rolling window for candle-body pressure
_PRESSURE_MIN    = 7
_PRESSURE_BULL   = 0.6     # fraction of bullish bodies ≥ this → buyer pressure (+1)
_PRESSURE_BEAR   = 0.4     # fraction of bullish bodies < this → seller pressure (−1)
_SESSION_HIGH    = 0.85    # position_in_day_range ≥ this → near high (+1)
_SESSION_LOW     = 0.15    # position_in_day_range ≤ this → near low (−1)


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df.get(col, pd.Series(np.nan, index=df.index)), errors="coerce")


def add_direction_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add 6 direction columns to a single-day frame already carrying compression features.

    Expects: ``close, high, low, vwap, ema_order, position_in_day_range`` columns.
    Frame must be a single trade_date sorted ascending by timestamp.
    """
    close  = _num(df, "close")
    high   = _num(df, "high")
    low    = _num(df, "low")
    vwap   = _num(df, "vwap")
    open_  = _num(df, "open") if "open" in df.columns else close.shift(1)

    # ── 1. Price structure (±2) ─────────────────────────────────────────────
    # rising_lows_frac: fraction of bars in last N where low[i] > low[i-1]
    # falling_highs_frac: fraction where high[i] < high[i-1]
    rising_lows    = (low  > low.shift(1)).astype(float)
    falling_highs  = (high < high.shift(1)).astype(float)
    rl_frac = rising_lows.rolling(_STRUCTURE_N, min_periods=_STRUCTURE_MIN).mean()
    fh_frac = falling_highs.rolling(_STRUCTURE_N, min_periods=_STRUCTURE_MIN).mean()
    # Net: how much more bullish structure than bearish?
    net = rl_frac - fh_frac   # > 0 → higher lows dominate; < 0 → lower highs dominate
    structure = pd.Series(0.0, index=df.index)
    structure = structure.mask(net > (_STRUCTURE_BULL - 0.5), 2.0)
    structure = structure.mask(net < -(_STRUCTURE_BEAR - 0.5), -2.0)
    # NaN where insufficient history
    structure = structure.where(rl_frac.notna() | fh_frac.notna(), np.nan)
    df["dir_structure"] = structure

    # ── 2. VWAP acceptance (±2) ─────────────────────────────────────────────
    # 3 consecutive closes above VWAP = +2 (price accepting higher levels)
    # 3 consecutive closes below VWAP = −2 (price accepting lower levels)
    above_vwap = (close > vwap).astype(float).where(vwap.notna() & close.notna(), np.nan)
    below_vwap = (close < vwap).astype(float).where(vwap.notna() & close.notna(), np.nan)
    # rolling min = 1.0 only when ALL N bars are True
    consec_above = above_vwap.rolling(_VWAP_CONSEC_N, min_periods=_VWAP_CONSEC_N).min()
    consec_below = below_vwap.rolling(_VWAP_CONSEC_N, min_periods=_VWAP_CONSEC_N).min()
    vwap_hold = pd.Series(0.0, index=df.index)
    vwap_hold = vwap_hold.mask(consec_above == 1.0,  2.0)
    vwap_hold = vwap_hold.mask(consec_below == 1.0, -2.0)
    vwap_hold = vwap_hold.where(above_vwap.notna(), np.nan)
    df["dir_vwap_hold"] = vwap_hold

    # ── 3. EMA quality (±1) ─────────────────────────────────────────────────
    # Reuse ema_order from compression_features: +1 (bull) / -1 (bear) / 0 (mixed)
    ema_order = _num(df, "ema_order")
    df["dir_ema"] = ema_order.clip(lower=-1.0, upper=1.0)

    # ── 4. Pressure score (±1) ──────────────────────────────────────────────
    # Candle body direction: close > open = bullish bar (buyers defended the move)
    bullish_body = (close > open_).astype(float).where(close.notna() & open_.notna(), np.nan)
    body_frac = bullish_body.rolling(_PRESSURE_N, min_periods=_PRESSURE_MIN).mean()
    pressure = pd.Series(0.0, index=df.index)
    pressure = pressure.mask(body_frac >= _PRESSURE_BULL,  1.0)
    pressure = pressure.mask(body_frac <= _PRESSURE_BEAR, -1.0)
    pressure = pressure.where(body_frac.notna(), np.nan)
    df["dir_pressure"] = pressure

    # ── 5. Session position (±1) ─────────────────────────────────────────────
    # position_in_day_range from compression_features: (close - day_low) / (day_high - day_low)
    pos = _num(df, "position_in_day_range")
    sess_pos = pd.Series(0.0, index=df.index)
    sess_pos = sess_pos.mask(pos >= _SESSION_HIGH,  1.0)
    sess_pos = sess_pos.mask(pos <= _SESSION_LOW,  -1.0)
    sess_pos = sess_pos.where(pos.notna(), np.nan)
    df["dir_session_pos"] = sess_pos

    # ── 6. Weighted total score (−7 to +7) ──────────────────────────────────
    components = pd.concat(
        [structure, vwap_hold, df["dir_ema"], pressure, sess_pos],
        axis=1,
    )
    df["dir_score"] = components.sum(axis=1, min_count=3)  # require ≥3 of 5 valid
    return df


def add_direction_features_from_flat(df: pd.DataFrame) -> pd.DataFrame:
    """Historical flat-dataset entry point (px_fut_* naming).

    Ensures compression features are present (for ema_order / position_in_day_range),
    then maps to standard names and calls :func:`add_direction_features`.
    Frame must be a single trade_date sorted ascending by timestamp.
    """
    # Ensure compression features are present (idempotent check)
    from .compression_features import COMPRESSION_FEATURE_COLUMNS
    if "ema_order" not in df.columns:
        df = add_compression_features_from_flat(df)

    close = _num(df, "px_fut_close")
    high  = _num(df, "px_fut_high")
    low   = _num(df, "px_fut_low")
    open_ = _num(df, "px_fut_open") if "px_fut_open" in df.columns else close.shift(1)
    # VWAP: prefer pre-computed vwap_fut, else compute causal session VWAP
    if "vwap_fut" in df.columns:
        vwap = _num(df, "vwap_fut")
    else:
        tp = (high + low + close) / 3.0
        vol = _num(df, "px_fut_volume") if "px_fut_volume" in df.columns else pd.Series(1.0, index=df.index)
        cum_pv = (tp * vol).cumsum()
        cum_v  = vol.cumsum().replace(0.0, np.nan)
        vwap   = cum_pv / cum_v

    tmp = pd.DataFrame(index=df.index)
    tmp["close"]              = close
    tmp["high"]               = high
    tmp["low"]                = low
    tmp["open"]               = open_
    tmp["vwap"]               = vwap
    tmp["ema_order"]          = df["ema_order"].to_numpy()
    tmp["position_in_day_range"] = df["position_in_day_range"].to_numpy()

    add_direction_features(tmp)
    for col in DIRECTION_FEATURE_COLUMNS:
        df[col] = tmp[col].to_numpy()
    return df


def direction_label(score: float) -> str:
    """Human-readable label from a dir_score value."""
    if score >= 5:
        return "STRONG_LONG"
    if score >= 3:
        return "LONG"
    if score <= -5:
        return "STRONG_SHORT"
    if score <= -3:
        return "SHORT"
    return "NO_DIRECTION"
