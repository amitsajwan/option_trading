from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


def _first_numeric_series(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    for column in columns:
        if column in frame.columns:
            return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype=float)


def attach_regime_features(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    vix = _first_numeric_series(out, ["vix_prev_close"])
    ema_spread = _first_numeric_series(out, ["ema_9_21_spread"])
    dte_days = _first_numeric_series(out, ["dte_days", "ctx_dte_days"])
    ctx_high_vix = _first_numeric_series(out, ["ctx_is_high_vix_day"])
    ctx_atr_high = _first_numeric_series(out, ["ctx_regime_atr_high"])
    ctx_atr_low = _first_numeric_series(out, ["ctx_regime_atr_low"])
    ctx_trend_up = _first_numeric_series(out, ["ctx_regime_trend_up"])
    ctx_trend_down = _first_numeric_series(out, ["ctx_regime_trend_down"])
    ctx_expiry_near = _first_numeric_series(out, ["ctx_regime_expiry_near"])
    atr_pct = _first_numeric_series(out, ["atr_daily_percentile", "osc_atr_daily_percentile", "atr_percentile", "osc_atr_percentile"])
    vol_high = pd.Series(np.where(vix.notna(), (vix >= 20.0).astype(float), np.nan), index=out.index)
    if ctx_high_vix.notna().any():
        vol_high = ctx_high_vix.where(ctx_high_vix.notna(), vol_high)
    out["regime_vol_high"] = vol_high
    out["regime_vol_low"] = pd.Series(np.where(vix.notna(), (vix < 16.0).astype(float), np.nan), index=out.index)
    out["regime_vol_neutral"] = pd.Series(np.where(vix.notna(), ((vix >= 16.0) & (vix < 20.0)).astype(float), np.nan), index=out.index)
    out["regime_atr_high"] = ctx_atr_high.where(ctx_atr_high.notna(), pd.Series(np.where(atr_pct.notna(), (atr_pct >= 0.70).astype(float), np.nan), index=out.index))
    out["regime_atr_low"] = ctx_atr_low.where(ctx_atr_low.notna(), pd.Series(np.where(atr_pct.notna(), (atr_pct <= 0.30).astype(float), np.nan), index=out.index))
    out["regime_trend_up"] = ctx_trend_up.where(ctx_trend_up.notna(), pd.Series(np.where(ema_spread.notna(), (ema_spread > 0.0).astype(float), np.nan), index=out.index))
    out["regime_trend_down"] = ctx_trend_down.where(ctx_trend_down.notna(), pd.Series(np.where(ema_spread.notna(), (ema_spread < 0.0).astype(float), np.nan), index=out.index))
    out["regime_expiry_near"] = ctx_expiry_near.where(ctx_expiry_near.notna(), pd.Series(np.where(dte_days.notna(), ((dte_days >= 0) & (dte_days <= 1)).astype(float), np.nan), index=out.index))
    return out


def summarize_regimes(frame: pd.DataFrame) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for key in (
        "regime_vol_high",
        "regime_vol_low",
        "regime_vol_neutral",
        "regime_atr_high",
        "regime_atr_low",
        "regime_trend_up",
        "regime_trend_down",
        "regime_expiry_near",
    ):
        out[key] = float(pd.to_numeric(frame.get(key), errors="coerce").fillna(0.0).mean()) if key in frame.columns and len(frame) else 0.0
    return out

