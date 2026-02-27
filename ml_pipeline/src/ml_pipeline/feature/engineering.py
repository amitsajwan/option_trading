import argparse
from pathlib import Path
from typing import Iterable, List, Optional, Union

import numpy as np
import pandas as pd

from .regime import attach_regime_features
from ..vix_data import load_vix_daily

_DEPTH_BASE_COLUMNS = (
    "depth_total_bid_qty",
    "depth_total_ask_qty",
    "depth_top_bid_qty",
    "depth_top_ask_qty",
    "depth_top_bid_price",
    "depth_top_ask_price",
)


def _first_existing(columns: List[str], candidates: List[str]) -> Optional[str]:
    for col in candidates:
        if col in columns:
            return col
    return None


def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0)).abs()
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["fut_close"].shift(1)
    tr = pd.concat(
        [
            (df["fut_high"] - df["fut_low"]).abs(),
            (df["fut_high"] - prev_close).abs(),
            (df["fut_low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _session_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["fut_high"] + df["fut_low"] + df["fut_close"]) / 3.0
    pv = typical * df["fut_volume"].fillna(0.0)
    cumulative_pv = pv.cumsum()
    cumulative_volume = df["fut_volume"].fillna(0.0).cumsum()
    return cumulative_pv / cumulative_volume.replace(0.0, np.nan)


def _opening_range_features(group: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    out = group.copy()
    out["minute_index"] = np.arange(len(out))
    # Use timestamp-aware window: from session open to open + N minutes.
    # head(N) is wrong when there are pre-market bars, gaps, or irregular cadence.
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
    out["opening_range_breakout_up"] = (
        ready & (out["fut_close"] > out["opening_range_high"])
    ).astype(int)
    out["opening_range_breakout_down"] = (
        ready & (out["fut_close"] < out["opening_range_low"])
    ).astype(int)
    return out


def _add_depth_features(out: pd.DataFrame) -> pd.DataFrame:
    if not all(col in out.columns for col in _DEPTH_BASE_COLUMNS):
        return out

    bid = out["depth_total_bid_qty"].astype(float)
    ask = out["depth_total_ask_qty"].astype(float)
    top_bid = out["depth_top_bid_qty"].astype(float)
    top_ask = out["depth_top_ask_qty"].astype(float)
    bid_px = out["depth_top_bid_price"].astype(float)
    ask_px = out["depth_top_ask_price"].astype(float)

    out["depth_bid_ask_ratio"] = bid / ask.replace(0.0, np.nan)
    denom = (bid + ask).replace(0.0, np.nan)
    out["depth_imbalance"] = (bid - ask) / denom
    out["depth_top_level_ratio"] = top_bid / top_ask.replace(0.0, np.nan)
    out["depth_spread"] = ask_px - bid_px
    out["depth_spread_bps"] = (out["depth_spread"] / out["fut_close"].replace(0.0, np.nan)) * 10000.0
    out["depth_imbalance_change_1m"] = out["depth_imbalance"].diff()
    return out


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
    # NOTE: atr_percentile is intentionally left as a within-session expanding rank here.
    # A cross-session daily ATR percentile is computed separately in build_feature_table()
    # and joined back as atr_daily_percentile. This intraday version is kept for
    # session-level context (how volatile is this bar vs earlier today).
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
    out["fut_volume_accel_1m"] = (
        out["fut_volume"].pct_change(1, fill_method=None)
        .replace([np.inf, -np.inf], np.nan)  # prev bar vol=0 → inf
    )

    out["atm_call_return_1m"] = out["opt_0_ce_close"].pct_change(1, fill_method=None)
    out["atm_put_return_1m"] = out["opt_0_pe_close"].pct_change(1, fill_method=None)
    out["atm_oi_change_1m"] = (out["opt_0_ce_oi"] + out["opt_0_pe_oi"]).diff()
    out["ce_pe_oi_diff"] = out["ce_oi_total"] - out["pe_oi_total"]
    out["ce_pe_volume_diff"] = out["ce_volume_total"] - out["pe_volume_total"]
    out["options_volume_total"] = out["ce_volume_total"] + out["pe_volume_total"]
    opt_vol_roll = out["options_volume_total"].rolling(20, min_periods=5).mean()
    out["options_rel_volume_20"] = out["options_volume_total"] / opt_vol_roll.replace(0.0, np.nan)

    ce_iv_col = _first_existing(
        list(out.columns),
        ["opt_0_ce_iv", "atm_ce_iv", "atm_call_iv", "ce_iv"],
    )
    pe_iv_col = _first_existing(
        list(out.columns),
        ["opt_0_pe_iv", "atm_pe_iv", "atm_put_iv", "pe_iv"],
    )
    if ce_iv_col is not None and pe_iv_col is not None:
        ce_iv = pd.to_numeric(out[ce_iv_col], errors="coerce")
        pe_iv = pd.to_numeric(out[pe_iv_col], errors="coerce")
        out["atm_iv"] = (ce_iv + pe_iv) / 2.0
        out["iv_skew"] = (ce_iv - pe_iv) / out["atm_iv"].replace(0.0, np.nan)
    else:
        if "atm_iv" not in out.columns:
            out["atm_iv"] = np.nan
        if "iv_skew" not in out.columns:
            out["iv_skew"] = np.nan

    out["minute_of_day"] = out["timestamp"].dt.hour * 60 + out["timestamp"].dt.minute
    out["day_of_week"] = out["timestamp"].dt.dayofweek

    out = _add_depth_features(out)
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
    dte = pd.to_numeric(dte, errors="coerce")
    dte = dte.where(dte >= 0, np.nan)
    out["dte_days"] = dte
    out["is_expiry_day"] = (out["dte_days"] == 0).astype(float)
    out["is_near_expiry"] = ((out["dte_days"] >= 0) & (out["dte_days"] <= 1)).astype(float)
    return out


def _add_vix_features(out: pd.DataFrame, vix_source: Optional[Union[str, Path]]) -> pd.DataFrame:
    if vix_source is None:
        out["vix_prev_close"] = np.nan
        out["vix_prev_close_change_1d"] = np.nan
        out["vix_prev_close_zscore_20d"] = np.nan
        out["is_high_vix_day"] = 0.0
        return out
    vix_daily = load_vix_daily(vix_source)
    if len(vix_daily) == 0:
        out["vix_prev_close"] = np.nan
        out["vix_prev_close_change_1d"] = np.nan
        out["vix_prev_close_zscore_20d"] = np.nan
        out["is_high_vix_day"] = 0.0
        return out
    aligned = vix_daily.copy()
    aligned["trade_date"] = pd.to_datetime(aligned["trade_date"], errors="coerce")
    aligned["vix_close"] = pd.to_numeric(aligned["vix_close"], errors="coerce")
    aligned = aligned.dropna(subset=["trade_date", "vix_close"]).sort_values("trade_date")
    if len(aligned) == 0:
        out["vix_prev_close"] = np.nan
        out["vix_prev_close_change_1d"] = np.nan
        out["vix_prev_close_zscore_20d"] = np.nan
        out["is_high_vix_day"] = 0.0
        return out

    # Compute prior-day close and derived stats from full VIX history so
    # single-day feature builds still have non-leaky previous-close context.
    aligned["vix_prev_close"] = aligned["vix_close"].shift(1)
    aligned["vix_prev_close_change_1d"] = aligned["vix_prev_close"].pct_change(fill_method=None)
    roll_mean = aligned["vix_prev_close"].rolling(20, min_periods=5).mean()
    roll_std = aligned["vix_prev_close"].rolling(20, min_periods=5).std(ddof=0).replace(0.0, np.nan)
    aligned["vix_prev_close_zscore_20d"] = (aligned["vix_prev_close"] - roll_mean) / roll_std
    aligned["is_high_vix_day"] = (aligned["vix_prev_close"] >= 20.0).astype(float)

    idx = aligned.set_index("trade_date")
    td = pd.to_datetime(out["trade_date"].astype(str), errors="coerce")
    out["vix_prev_close"] = td.map(idx["vix_prev_close"])
    out["vix_prev_close_change_1d"] = td.map(idx["vix_prev_close_change_1d"])
    out["vix_prev_close_zscore_20d"] = td.map(idx["vix_prev_close_zscore_20d"])
    out["is_high_vix_day"] = td.map(idx["is_high_vix_day"]).fillna(0.0).astype(float)
    return out


def _add_cross_session_atr_percentile(out: pd.DataFrame) -> pd.DataFrame:
    """Compute ATR percentile across sessions (expanding rank on daily ATR).

    The within-session atr_percentile computed in _add_group_features ranks
    each bar against earlier bars *in the same day* — it resets every session
    and is unreliable for the first 20 bars (the most important opening period).
    This function adds atr_daily_percentile: each bar's rank of its *session*
    ATR against all prior sessions, so the opening bar of a high-vol day
    already has a meaningful cross-session volatility context.
    """
    if "atr_14" not in out.columns or "trade_date" not in out.columns:
        out["atr_daily_percentile"] = np.nan
        return out
    # One ATR value per session: take the last bar's ATR (fully settled EWM)
    daily_atr = (
        out.groupby("trade_date", sort=True)["atr_14"]
        .last()
        .rename("daily_atr")
        .reset_index()
    )
    daily_atr["atr_daily_percentile"] = (
        daily_atr["daily_atr"].expanding(min_periods=5).rank(pct=True)
    )
    atr_pct_map = daily_atr.set_index("trade_date")["atr_daily_percentile"]
    atr_pct_by_trade_date = pd.Series(atr_pct_map.values, index=atr_pct_map.index.astype(str))
    out["atr_daily_percentile"] = out["trade_date"].astype(str).map(atr_pct_by_trade_date)
    return out


def build_feature_table(panel: pd.DataFrame, vix_source: Optional[Union[str, Path]] = None) -> pd.DataFrame:
    base = panel.copy()
    base["timestamp"] = pd.to_datetime(base["timestamp"], errors="coerce")
    base = base.dropna(subset=["timestamp"]).sort_values(["trade_date", "timestamp"]).reset_index(drop=True)
    groups: List[pd.DataFrame] = []
    for _, group in base.groupby("trade_date", sort=True):
        groups.append(_add_group_features(group))
    if not groups:
        return pd.DataFrame()
    out = pd.concat(groups, ignore_index=True)
    out = _add_dte_features(out)
    out = _add_vix_features(out, vix_source=vix_source)
    out = _add_cross_session_atr_percentile(out)
    out = attach_regime_features(out)
    out = out.sort_values(["timestamp"]).reset_index(drop=True)
    return out


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build leakage-safe feature table from canonical panel")
    parser.add_argument(
        "--panel",
        default="ml_pipeline/artifacts/t03_canonical_panel.parquet",
        help="Input panel parquet path",
    )
    parser.add_argument(
        "--out",
        default="ml_pipeline/artifacts/t04_features.parquet",
        help="Output feature table parquet path",
    )
    parser.add_argument(
        "--vix-path",
        default=None,
        help="Optional VIX source file or directory with NSE historical CSV(s)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    panel_path = Path(args.panel)
    if not panel_path.exists():
        print(f"ERROR: panel file not found: {panel_path}")
        return 2

    panel = pd.read_parquet(panel_path)
    features = build_feature_table(panel, vix_source=args.vix_path)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    features.to_parquet(out_path, index=False)

    print(f"Input panel: {panel_path}")
    print(f"Rows in: {len(panel)}")
    print(f"Rows out: {len(features)}")
    print(f"Columns out: {len(features.columns)}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
