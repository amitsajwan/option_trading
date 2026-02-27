from typing import Dict

import numpy as np
import pandas as pd


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column in frame.columns:
        return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype=float)


def attach_regime_features(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    vix = _numeric_series(out, "vix_prev_close")
    ema_spread = _numeric_series(out, "ema_9_21_spread")
    dte_days = _numeric_series(out, "dte_days")

    # Prefer cross-session daily ATR percentile (consistent from first bar of day).
    # Fall back to intraday atr_percentile if atr_daily_percentile not present.
    if "atr_daily_percentile" in out.columns:
        atr_daily = pd.to_numeric(out["atr_daily_percentile"], errors="coerce")
        atr_intraday = _numeric_series(out, "atr_percentile")
        atr_pct = atr_daily.where(atr_daily.notna(), atr_intraday)
    else:
        atr_pct = _numeric_series(out, "atr_percentile")

    out["regime_vol_high"] = pd.Series(
        np.where(vix.notna(), (vix >= 20.0).astype(float), np.nan),
        index=out.index,
    )
    out["regime_vol_low"] = pd.Series(
        np.where(vix.notna(), (vix < 16.0).astype(float), np.nan),
        index=out.index,
    )
    # Explicit neutral band so the model doesn't have to infer by exclusion.
    out["regime_vol_neutral"] = pd.Series(
        np.where(vix.notna(), ((vix >= 16.0) & (vix < 20.0)).astype(float), np.nan),
        index=out.index,
    )
    out["regime_atr_high"] = pd.Series(
        np.where(atr_pct.notna(), (atr_pct >= 0.70).astype(float), np.nan),
        index=out.index,
    )
    out["regime_atr_low"] = pd.Series(
        np.where(atr_pct.notna(), (atr_pct <= 0.30).astype(float), np.nan),
        index=out.index,
    )
    out["regime_trend_up"] = pd.Series(
        np.where(ema_spread.notna(), (ema_spread > 0.0).astype(float), np.nan),
        index=out.index,
    )
    out["regime_trend_down"] = pd.Series(
        np.where(ema_spread.notna(), (ema_spread < 0.0).astype(float), np.nan),
        index=out.index,
    )
    out["regime_expiry_near"] = pd.Series(
        np.where(dte_days.notna(), ((dte_days >= 0) & (dte_days <= 1)).astype(float), np.nan),
        index=out.index,
    )
    return out


def summarize_regimes(frame: pd.DataFrame) -> Dict[str, float]:
    keys = [
        "regime_vol_high",
        "regime_vol_low",
        "regime_vol_neutral",
        "regime_atr_high",
        "regime_atr_low",
        "regime_trend_up",
        "regime_trend_down",
        "regime_expiry_near",
    ]
    out: Dict[str, float] = {}
    n = float(len(frame))
    for key in keys:
        if key not in frame.columns or n <= 0:
            out[key] = 0.0
            continue
        out[key] = float(pd.to_numeric(frame[key], errors="coerce").fillna(0.0).mean())
    return out
