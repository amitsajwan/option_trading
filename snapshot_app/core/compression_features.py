"""Compression / stored-energy / structure features for the Big-Move Model (BMM).

Single source of truth used by BOTH the live snapshot path
(``market_snapshot.prepare_market_snapshot_window``) and the historical rebuild
(``rebuild_stage_views_from_flat``) so there is ZERO train/serve skew — the same
column values the model trains on are the ones it sees live.

Every feature is **causal** (uses only the current and prior completed bars):
- rolling windows use ``min_periods`` and never look forward,
- "is it compressed vs its own recent average?" baselines are ``.shift(1)`` so the
  current bar is compared against the PRIOR window, not itself,
- ``day_high`` / ``day_low`` are expanding (cummax / cummin) — already causal.

The function operates on a single trade-date frame, sorted ascending by time, that
already carries the standard internal column names produced upstream:
``close, high, low, ema_9, ema_21, ema_50, atr_ratio, day_high, day_low``.
For the historical flat dataset (``px_fut_*`` naming) use :func:`add_compression_features_from_flat`.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd

# Public column contract — the exact feature names emitted. Keep in sync with the
# stage_views spec (futures_derived block) and the fo_bmm_v1 feature set regex.
COMPRESSION_FEATURE_COLUMNS: Tuple[str, ...] = (
    "bb_width_20",            # Bollinger-band width fraction (2*std20/sma20) — compression level
    "bb_width_chg_5",         # 5-bar change in bb_width_20 — compression dynamics (squeeze building/releasing)
    "range_10",               # mean high-low range over last 10 bars
    "range_30",               # mean high-low range over last 30 bars
    "range_ratio_10_30",      # range_10 / range_30 (<1 = recent contraction)
    "candle_overlap_10",      # mean bar-to-bar overlap ratio over last 10 bars (consolidation)
    "ema_spread_9_21",        # (ema_9 - ema_21) / close — trend tightness/quality
    "ema_spread_21_50",       # (ema_21 - ema_50) / close
    "ema_order",              # +1 stacked bull, -1 stacked bear, 0 mixed (trend quality)
    "dist_from_ema21",        # (close - ema_21) / ema_21 — structure
    "position_in_day_range",  # (close - day_low) / (day_high - day_low) in [0,1]
    "compression_score",      # raw 0..4 count of compression conditions (NOT a gate — fed raw)
)

_EPS = 1e-12
_BB_PERIOD = 20
_RANGE_SHORT = 10
_RANGE_LONG = 30
_OVERLAP_WINDOW = 10
_BASELINE_WINDOW = 20
_EMA_TIGHT = 0.0008          # |ema9-ema21|/close below this == compressed (matches E1 harness)
_RANGE_CONTRACT = 0.6        # range_ratio below this == contracted (matches E1 harness)


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype="float64")


def add_compression_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add the BMM compression/structure columns IN PLACE (returns the same frame).

    Expects standard internal names: close, high, low, ema_9, ema_21, ema_50,
    atr_ratio, day_high, day_low. Missing inputs degrade to NaN for the derived
    column rather than raising — callers must tolerate NaN (the trainer imputes).
    Frame MUST be a single trade_date, sorted ascending by time.
    """
    close = _num(df, "close")
    high = _num(df, "high")
    low = _num(df, "low")
    ema9 = _num(df, "ema_9")
    ema21 = _num(df, "ema_21")
    ema50 = _num(df, "ema_50")
    atr_ratio = _num(df, "atr_ratio")
    day_high = _num(df, "day_high")
    day_low = _num(df, "day_low")

    # --- Bollinger width + dynamics ---
    sma20 = close.rolling(_BB_PERIOD, min_periods=_BB_PERIOD).mean()
    std20 = close.rolling(_BB_PERIOD, min_periods=_BB_PERIOD).std(ddof=0)
    bb_width_20 = (2.0 * std20 / sma20.replace(0.0, np.nan))
    df["bb_width_20"] = bb_width_20
    df["bb_width_chg_5"] = bb_width_20 - bb_width_20.shift(5)

    # --- Range contraction ---
    rng = (high - low)
    range_10 = rng.rolling(_RANGE_SHORT, min_periods=_RANGE_SHORT).mean()
    range_30 = rng.rolling(_RANGE_LONG, min_periods=_RANGE_LONG).mean()
    df["range_10"] = range_10
    df["range_30"] = range_30
    range_ratio = range_10 / range_30.replace(0.0, np.nan)
    df["range_ratio_10_30"] = range_ratio

    # --- Candle overlap (consolidation): overlap of [low,high] with the PRIOR bar ---
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    inter = (np.minimum(high, prev_high) - np.maximum(low, prev_low)).clip(lower=0.0)
    union = (np.maximum(high, prev_high) - np.minimum(low, prev_low)).replace(0.0, np.nan)
    overlap = (inter / union)
    df["candle_overlap_10"] = overlap.rolling(_OVERLAP_WINDOW, min_periods=_OVERLAP_WINDOW).mean()

    # --- EMA structure / trend quality ---
    safe_close = close.replace(0.0, np.nan)
    ema_spread_9_21 = (ema9 - ema21) / safe_close
    ema_spread_21_50 = (ema21 - ema50) / safe_close
    df["ema_spread_9_21"] = ema_spread_9_21
    df["ema_spread_21_50"] = ema_spread_21_50
    order = pd.Series(0.0, index=df.index, dtype="float64")
    order = order.mask((ema9 > ema21) & (ema21 > ema50), 1.0)
    order = order.mask((ema9 < ema21) & (ema21 < ema50), -1.0)
    # leave NaN where EMAs unavailable
    order = order.where(ema9.notna() & ema21.notna() & ema50.notna(), np.nan)
    df["ema_order"] = order
    df["dist_from_ema21"] = (close - ema21) / ema21.replace(0.0, np.nan)

    # --- Position in day range ---
    day_span = (day_high - day_low).replace(0.0, np.nan)
    df["position_in_day_range"] = ((close - day_low) / day_span).clip(lower=0.0, upper=1.0)

    # --- Raw compression score (0..4): count of contraction conditions, causal baselines ---
    bb_base = bb_width_20.rolling(_BASELINE_WINDOW, min_periods=_BASELINE_WINDOW).mean().shift(1)
    atr_base = atr_ratio.rolling(_BASELINE_WINDOW, min_periods=_BASELINE_WINDOW).mean().shift(1)
    c1 = (bb_width_20 < bb_base).astype("float64")
    c2 = (atr_ratio < atr_base).astype("float64")
    c3 = (range_ratio < _RANGE_CONTRACT).astype("float64")
    c4 = (ema_spread_9_21.abs() < _EMA_TIGHT).astype("float64")
    # If all inputs NaN, score stays NaN; otherwise sum available conditions.
    stack = pd.concat([c1, c2, c3, c4], axis=1)
    valid_any = pd.concat(
        [bb_width_20.notna() & bb_base.notna(), atr_ratio.notna() & atr_base.notna(),
         range_ratio.notna(), ema_spread_9_21.notna()],
        axis=1,
    ).any(axis=1)
    score = stack.sum(axis=1, min_count=1)
    df["compression_score"] = score.where(valid_any, np.nan)

    return df


def add_compression_features_from_flat(df: pd.DataFrame) -> pd.DataFrame:
    """Historical flat-dataset entry point (px_fut_* naming).

    Computes ema_9/21/50 and day_high/day_low/atr_ratio from the flat OHLC if they
    are not already present, maps to standard names, runs :func:`add_compression_features`,
    and leaves the new columns on the frame. Frame MUST be a single trade_date sorted
    ascending by timestamp.
    """
    work = df  # operate in place; we only add columns
    close = _num(df, "px_fut_close")
    high = _num(df, "px_fut_high")
    low = _num(df, "px_fut_low")

    tmp = pd.DataFrame(index=df.index)
    tmp["close"] = close
    tmp["high"] = high
    tmp["low"] = low
    # EMAs: prefer existing flat columns, else compute causally from close.
    tmp["ema_9"] = _num(df, "ema_9") if "ema_9" in df.columns else close.ewm(span=9, adjust=False, min_periods=9).mean()
    tmp["ema_21"] = _num(df, "ema_21") if "ema_21" in df.columns else close.ewm(span=21, adjust=False, min_periods=21).mean()
    tmp["ema_50"] = _num(df, "ema_50") if "ema_50" in df.columns else close.ewm(span=50, adjust=False, min_periods=50).mean()
    # atr_ratio: prefer flat osc_atr_ratio, else NaN (c2 condition will be skipped).
    if "osc_atr_ratio" in df.columns:
        tmp["atr_ratio"] = _num(df, "osc_atr_ratio")
    elif "atr_ratio" in df.columns:
        tmp["atr_ratio"] = _num(df, "atr_ratio")
    else:
        tmp["atr_ratio"] = np.nan
    tmp["day_high"] = high.cummax()
    tmp["day_low"] = low.cummin()

    add_compression_features(tmp)
    for col in COMPRESSION_FEATURE_COLUMNS:
        work[col] = tmp[col].to_numpy()
    return work
