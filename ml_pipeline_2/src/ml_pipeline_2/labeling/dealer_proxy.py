from __future__ import annotations

import numpy as np
import pandas as pd


DEALER_PROXY_COLUMNS = (
    "dealer_proxy_oi_imbalance",
    "dealer_proxy_oi_imbalance_change_5m",
    "dealer_proxy_pcr_change_5m",
    "dealer_proxy_atm_oi_velocity_5m",
    "dealer_proxy_volume_imbalance",
)


def _first_numeric_series(frame: pd.DataFrame, candidates: list[str]) -> pd.Series:
    for candidate in candidates:
        if candidate in frame.columns:
            return pd.to_numeric(frame[candidate], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype=float)


def _existing_numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def attach_dealer_proxy_features(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if all(column in out.columns for column in DEALER_PROXY_COLUMNS):
        return out

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

    oi_imbalance = _existing_numeric_series(out, "dealer_proxy_oi_imbalance")
    oi_imbalance = oi_imbalance.where(oi_imbalance.notna(), ((ce_oi - pe_oi) / oi_total) * expiry_weight * vix_weight)
    if "dealer_proxy_oi_imbalance" not in out.columns:
        out["dealer_proxy_oi_imbalance"] = oi_imbalance

    if "dealer_proxy_oi_imbalance_change_5m" not in out.columns:
        if "trade_date" in out.columns:
            out["dealer_proxy_oi_imbalance_change_5m"] = oi_imbalance.groupby(out["trade_date"], sort=False).diff(5)
        else:
            out["dealer_proxy_oi_imbalance_change_5m"] = oi_imbalance.diff(5)

    if "dealer_proxy_pcr_change_5m" not in out.columns:
        pcr_weighted = pcr * vix_weight
        if "trade_date" in out.columns:
            out["dealer_proxy_pcr_change_5m"] = pcr_weighted.groupby(out["trade_date"], sort=False).diff(5)
        else:
            out["dealer_proxy_pcr_change_5m"] = pcr_weighted.diff(5)

    if "dealer_proxy_atm_oi_velocity_5m" not in out.columns:
        atm_oi_weighted = atm_oi_change * expiry_weight
        if "trade_date" in out.columns:
            out["dealer_proxy_atm_oi_velocity_5m"] = (
                atm_oi_weighted.groupby(out["trade_date"], sort=False)
                .rolling(5, min_periods=1)
                .sum()
                .reset_index(level=0, drop=True)
                .reindex(out.index)
            )
        else:
            out["dealer_proxy_atm_oi_velocity_5m"] = atm_oi_weighted.rolling(5, min_periods=1).sum()

    if "dealer_proxy_volume_imbalance" not in out.columns:
        out["dealer_proxy_volume_imbalance"] = (ce_pe_volume_diff / volume_total) * vix_weight

    return out
