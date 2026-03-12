from __future__ import annotations

import pandas as pd

from .dealer_proxy import attach_dealer_proxy_features
from .regime import attach_regime_features


def _coerce_timestamp_order(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "timestamp" in out.columns:
        out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
        out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if "trade_date" not in out.columns and "timestamp" in out.columns:
        out["trade_date"] = out["timestamp"].dt.strftime("%Y-%m-%d")
    return out


def _derive_ctx_expiry_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "ctx_dte_days" not in out.columns and {"expiry_code", "trade_date"}.issubset(out.columns):
        expiry = pd.to_datetime(out["expiry_code"], format="%d%b%y", errors="coerce")
        trade_date = pd.to_datetime(out["trade_date"], errors="coerce")
        dte = (expiry.dt.normalize() - trade_date.dt.normalize()).dt.days
        out["ctx_dte_days"] = pd.to_numeric(dte, errors="coerce").where(lambda s: s >= 0)
    if "ctx_dte_days" in out.columns:
        ctx_dte = pd.to_numeric(out["ctx_dte_days"], errors="coerce")
        if "ctx_is_expiry_day" not in out.columns:
            out["ctx_is_expiry_day"] = (ctx_dte == 0).astype(float)
        if "ctx_is_near_expiry" not in out.columns:
            out["ctx_is_near_expiry"] = ((ctx_dte >= 0) & (ctx_dte <= 1)).astype(float)
    return out


def _derive_ctx_vix_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "ctx_is_high_vix_day" not in out.columns and "vix_prev_close" in out.columns:
        out["ctx_is_high_vix_day"] = (pd.to_numeric(out["vix_prev_close"], errors="coerce") >= 20.0).astype(float)
    return out


def _backfill_ctx_regime_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for ctx_col, regime_col in {
        "ctx_regime_trend_up": "regime_trend_up",
        "ctx_regime_trend_down": "regime_trend_down",
        "ctx_regime_atr_high": "regime_atr_high",
        "ctx_regime_atr_low": "regime_atr_low",
        "ctx_regime_expiry_near": "regime_expiry_near",
    }.items():
        if ctx_col not in out.columns and regime_col in out.columns:
            out[ctx_col] = pd.to_numeric(out[regime_col], errors="coerce")
    return out


def prepare_snapshot_labeled_frame(frame: pd.DataFrame, *, context: str) -> pd.DataFrame:
    if "timestamp" not in frame.columns:
        raise ValueError(f"{context} requires timestamp column")
    out = _coerce_timestamp_order(frame)
    out = _derive_ctx_expiry_columns(out)
    out = _derive_ctx_vix_columns(out)
    out = attach_dealer_proxy_features(out)
    out = attach_regime_features(out)
    out = _backfill_ctx_regime_columns(out)
    return out
