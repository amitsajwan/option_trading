from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd


def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0)).abs()
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = frame["fut_close"].shift(1)
    tr = pd.concat(
        [
            (frame["fut_high"] - frame["fut_low"]).abs(),
            (frame["fut_high"] - prev_close).abs(),
            (frame["fut_low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _session_vwap(frame: pd.DataFrame) -> pd.Series:
    typical = (frame["fut_high"] + frame["fut_low"] + frame["fut_close"]) / 3.0
    pv = typical * frame["fut_volume"].fillna(0.0)
    cumulative_pv = pv.cumsum()
    cumulative_volume = frame["fut_volume"].fillna(0.0).cumsum()
    return cumulative_pv / cumulative_volume.replace(0.0, np.nan)


def _opening_range_features(group: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    out = group.copy()
    out["minute_index"] = np.arange(len(out))
    if "timestamp" in out.columns and pd.api.types.is_datetime64_any_dtype(out["timestamp"]):
        session_open = out["timestamp"].iloc[0]
        cutoff = session_open + pd.Timedelta(minutes=minutes)
        first_window = out[out["timestamp"] <= cutoff]
    else:
        first_window = out.head(minutes)
    if first_window.empty:
        out["opening_range_high"] = np.nan
        out["opening_range_low"] = np.nan
    else:
        out["opening_range_high"] = float(first_window["fut_high"].max())
        out["opening_range_low"] = float(first_window["fut_low"].min())
    ready = out["minute_index"] >= minutes
    out["opening_range_ready"] = ready.astype(int)
    out["opening_range_breakout_up"] = (ready & (out["fut_close"] > out["opening_range_high"])).astype(int)
    out["opening_range_breakout_down"] = (ready & (out["fut_close"] < out["opening_range_low"])).astype(int)
    return out


def _first_numeric_series(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    for column in columns:
        if column in frame.columns:
            return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype=float)


def _add_group_features(group: pd.DataFrame) -> pd.DataFrame:
    out = group.sort_values("timestamp").copy()
    out["ret_1m"] = out["fut_close"].pct_change(1, fill_method=None)
    out["ret_3m"] = out["fut_close"].pct_change(3, fill_method=None)
    out["ret_5m"] = out["fut_close"].pct_change(5, fill_method=None)

    out["ema_9"] = out["fut_close"].ewm(span=9, adjust=False).mean()
    out["ema_21"] = out["fut_close"].ewm(span=21, adjust=False).mean()
    out["ema_50"] = out["fut_close"].ewm(span=50, adjust=False).mean()
    out["ema_9_21_spread"] = out["ema_9"] - out["ema_21"]
    out["ema_9_slope"] = out["ema_9"].diff()
    out["ema_21_slope"] = out["ema_21"].diff()
    out["ema_50_slope"] = out["ema_50"].diff()

    out["rsi_14"] = _compute_rsi(out["fut_close"], period=14)
    out["atr_14"] = _compute_atr(out, period=14)
    out["atr_ratio"] = out["atr_14"] / out["fut_close"].replace(0.0, np.nan)
    out["atr_percentile"] = out["atr_ratio"].expanding(min_periods=20).rank(pct=True)

    out["fut_vwap"] = _session_vwap(out)
    out["vwap_distance"] = (out["fut_close"] - out["fut_vwap"]) / out["fut_vwap"].replace(0.0, np.nan)

    running_high = out["fut_high"].cummax()
    running_low = out["fut_low"].cummin()
    out["distance_from_day_high"] = (out["fut_close"] - running_high) / running_high.replace(0.0, np.nan)
    out["distance_from_day_low"] = (out["fut_close"] - running_low) / running_low.replace(0.0, np.nan)

    out["basis"] = out["fut_close"] - out["spot_close"]
    out["basis_change_1m"] = out["basis"].diff()
    vol_roll = out["fut_volume"].rolling(20, min_periods=5).mean()
    out["fut_rel_volume_20"] = out["fut_volume"] / vol_roll.replace(0.0, np.nan)
    out["fut_volume_accel_1m"] = out["fut_volume"].pct_change(1, fill_method=None).replace([np.inf, -np.inf], np.nan)

    oi_roll = out["fut_oi"].rolling(20, min_periods=5).mean()
    out["fut_oi_change_1m"] = out["fut_oi"].diff(1)
    out["fut_oi_change_5m"] = out["fut_oi"].diff(5)
    out["fut_oi_rel_20"] = out["fut_oi"] / oi_roll.replace(0.0, np.nan)
    oi_std = out["fut_oi"].rolling(20, min_periods=5).std(ddof=0).replace(0.0, np.nan)
    out["fut_oi_zscore_20"] = (out["fut_oi"] - oi_roll) / oi_std

    atm_strike = _first_numeric_series(out, ["opt_flow_atm_strike", "atm_strike"])
    same_atm_as_prev = atm_strike.notna() & atm_strike.eq(atm_strike.shift(1))
    out["atm_call_return_1m"] = out["opt_0_ce_close"].pct_change(1, fill_method=None).where(same_atm_as_prev)
    out["atm_put_return_1m"] = out["opt_0_pe_close"].pct_change(1, fill_method=None).where(same_atm_as_prev)
    out["atm_oi_change_1m"] = ((out["opt_0_ce_oi"] + out["opt_0_pe_oi"]).diff()).where(same_atm_as_prev)
    out["ce_pe_oi_diff"] = out["ce_oi_total"] - out["pe_oi_total"]
    out["ce_pe_volume_diff"] = out["ce_volume_total"] - out["pe_volume_total"]
    out["options_volume_total"] = out["ce_volume_total"] + out["pe_volume_total"]
    opt_vol_roll = out["options_volume_total"].rolling(20, min_periods=5).mean()
    out["options_rel_volume_20"] = out["options_volume_total"] / opt_vol_roll.replace(0.0, np.nan)

    ce_iv = pd.to_numeric(out.get("opt_0_ce_iv"), errors="coerce") if "opt_0_ce_iv" in out.columns else pd.Series(np.nan, index=out.index)
    pe_iv = pd.to_numeric(out.get("opt_0_pe_iv"), errors="coerce") if "opt_0_pe_iv" in out.columns else pd.Series(np.nan, index=out.index)
    out["atm_iv"] = (ce_iv + pe_iv) / 2.0
    iv_denom = out["atm_iv"].abs().clip(lower=1e-4)
    out["iv_skew"] = ((ce_iv - pe_iv) / iv_denom).clip(lower=-10.0, upper=10.0)

    out["minute_of_day"] = out["timestamp"].dt.hour * 60 + out["timestamp"].dt.minute
    out["day_of_week"] = out["timestamp"].dt.dayofweek
    out = _opening_range_features(out, minutes=15)
    return out


def _add_dte_features(out: pd.DataFrame) -> pd.DataFrame:
    if "expiry_code" not in out.columns:
        out["dte_days"] = np.nan
        out["is_expiry_day"] = 0.0
        out["is_near_expiry"] = 0.0
        return out
    exp = pd.to_datetime(out["expiry_code"].astype(str).str.upper().str.strip(), format="%d%b%y", errors="coerce")
    td = pd.to_datetime(out["trade_date"].astype(str), errors="coerce")
    dte = (exp.dt.normalize() - td.dt.normalize()).dt.days
    dte = pd.to_numeric(dte, errors="coerce").where(lambda values: values >= 0, np.nan)
    out["dte_days"] = dte
    out["is_expiry_day"] = (out["dte_days"] == 0).astype(float)
    out["is_near_expiry"] = ((out["dte_days"] >= 0) & (out["dte_days"] <= 1)).astype(float)
    return out


def _add_vix_features(out: pd.DataFrame, vix_source: Optional[Union[str, Path]] = None) -> pd.DataFrame:
    del vix_source
    if "vix_prev_close" not in out.columns:
        out["vix_prev_close"] = np.nan
    else:
        out["vix_prev_close"] = pd.to_numeric(out["vix_prev_close"], errors="coerce")
    if "vix_prev_close_change_1d" not in out.columns:
        out["vix_prev_close_change_1d"] = np.nan
    else:
        out["vix_prev_close_change_1d"] = pd.to_numeric(out["vix_prev_close_change_1d"], errors="coerce")
    if "vix_prev_close_zscore_20d" not in out.columns:
        out["vix_prev_close_zscore_20d"] = np.nan
    else:
        out["vix_prev_close_zscore_20d"] = pd.to_numeric(out["vix_prev_close_zscore_20d"], errors="coerce")
    if "is_high_vix_day" not in out.columns:
        out["is_high_vix_day"] = 0.0
    else:
        out["is_high_vix_day"] = pd.to_numeric(out["is_high_vix_day"], errors="coerce").fillna(0.0).astype(float)
    return out


def _add_cross_session_atr_percentile(out: pd.DataFrame) -> pd.DataFrame:
    if "atr_14" not in out.columns or "trade_date" not in out.columns:
        out["atr_daily_percentile"] = np.nan
        return out
    daily_atr = out.groupby("trade_date", sort=True)["atr_14"].last().rename("daily_atr").reset_index()
    daily_atr["atr_daily_percentile"] = daily_atr["daily_atr"].expanding(min_periods=5).rank(pct=True)
    atr_pct_map = daily_atr.set_index("trade_date")["atr_daily_percentile"]
    out["atr_daily_percentile"] = out["trade_date"].astype(str).map(pd.Series(atr_pct_map.values, index=atr_pct_map.index.astype(str)))
    return out


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

    if "atr_daily_percentile" in out.columns or "osc_atr_daily_percentile" in out.columns:
        atr_daily = _first_numeric_series(out, ["atr_daily_percentile", "osc_atr_daily_percentile"])
        atr_intraday = _first_numeric_series(out, ["atr_percentile", "osc_atr_percentile"])
        atr_pct = atr_daily.where(atr_daily.notna(), atr_intraday)
    else:
        atr_pct = _first_numeric_series(out, ["atr_percentile", "osc_atr_percentile"])

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
    out["regime_expiry_near"] = ctx_expiry_near.where(
        ctx_expiry_near.notna(),
        pd.Series(np.where(dte_days.notna(), ((dte_days >= 0) & (dte_days <= 1)).astype(float), np.nan), index=out.index),
    )
    return out


def _add_dealer_proxy_features(out: pd.DataFrame) -> pd.DataFrame:
    ce_oi = _first_numeric_series(out, ["opt_flow_ce_oi_total", "ce_oi_total"])
    pe_oi = _first_numeric_series(out, ["opt_flow_pe_oi_total", "pe_oi_total"])
    pcr = _first_numeric_series(out, ["opt_flow_pcr_oi", "pcr_oi"])
    atm_oi_change = _first_numeric_series(out, ["opt_flow_atm_oi_change_1m", "atm_oi_change_1m"])
    ce_pe_volume_diff = _first_numeric_series(out, ["opt_flow_ce_pe_volume_diff", "ce_pe_volume_diff"])
    ce_volume = _first_numeric_series(out, ["opt_flow_ce_volume_total", "ce_volume_total"])
    pe_volume = _first_numeric_series(out, ["opt_flow_pe_volume_total", "pe_volume_total"])
    dte_days = _first_numeric_series(out, ["ctx_dte_days", "dte_days"]).clip(lower=0.0)
    high_vix = _first_numeric_series(out, ["ctx_is_high_vix_day", "is_high_vix_day"]).fillna(0.0)

    expiry_weight = 1.0 / (1.0 + dte_days.fillna(0.0))
    vix_weight = 1.0 + (0.25 * high_vix)
    oi_total = (ce_oi.abs() + pe_oi.abs()).replace(0.0, np.nan)
    volume_total = (ce_volume.abs() + pe_volume.abs()).replace(0.0, np.nan)
    raw_oi_imbalance = (ce_oi - pe_oi) / oi_total

    out["dealer_proxy_oi_imbalance"] = raw_oi_imbalance * expiry_weight * vix_weight
    if "trade_date" in out.columns:
        out["dealer_proxy_oi_imbalance_change_5m"] = out.groupby("trade_date", sort=False)["dealer_proxy_oi_imbalance"].diff(5)
    else:
        out["dealer_proxy_oi_imbalance_change_5m"] = out["dealer_proxy_oi_imbalance"].diff(5)
    pcr_weighted = pcr * vix_weight
    if "trade_date" in out.columns:
        out["dealer_proxy_pcr_change_5m"] = pcr_weighted.groupby(out["trade_date"]).diff(5)
        out["dealer_proxy_atm_oi_velocity_5m"] = (
            (atm_oi_change * expiry_weight).groupby(out["trade_date"]).rolling(5, min_periods=1).sum().reset_index(level=0, drop=True)
        )
    else:
        out["dealer_proxy_pcr_change_5m"] = pcr_weighted.diff(5)
        out["dealer_proxy_atm_oi_velocity_5m"] = (atm_oi_change * expiry_weight).rolling(5, min_periods=1).sum()
    out["dealer_proxy_volume_imbalance"] = (ce_pe_volume_diff / volume_total) * vix_weight
    return out


__all__ = [
    "_add_cross_session_atr_percentile",
    "_add_dealer_proxy_features",
    "_add_dte_features",
    "_add_group_features",
    "_add_vix_features",
    "attach_regime_features",
]
