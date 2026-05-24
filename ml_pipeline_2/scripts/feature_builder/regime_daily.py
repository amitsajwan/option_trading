"""Daily macro regime features for snapshots_ml_flat_v3.

All values for trade_date T use information through T-1 close only (shift(1)).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np
import pandas as pd

REGIME_COLUMNS: List[str] = [
    "regime_rv20",
    "regime_dist_sma20",
    "regime_sma20_slope",
    "regime_60d_return",
]

VIX_REGIME_COLUMNS: List[str] = [
    "regime_vix_close",
    "regime_vix_high",
]

ALL_REGIME_COLUMNS: List[str] = REGIME_COLUMNS + VIX_REGIME_COLUMNS

# Matches snapshot_batch ctx_is_high_vix_day (vix_prev_close >= 20)
VIX_HIGH_THRESHOLD = 20.0

TRADING_DAYS_PER_YEAR = 252
RV20_MIN_PERIODS = 10
SMA20_MIN_PERIODS = 10
SMA20_SLOPE_LAG = 5
RETURN_60D_LAG = 60


def resolve_parquet_root(explicit: Optional[str | Path] = None) -> Path:
    """Pick parquet_data root: CLI/env, VM default, then repo .data."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("OPTION_TRADING_PARQUET_ROOT", "").strip()
    candidates = [
        env,
        "/opt/option_trading/.data/ml_pipeline/parquet_data",
        Path(__file__).resolve().parents[3] / ".data" / "ml_pipeline" / "parquet_data",
    ]
    for raw in candidates:
        if not raw:
            continue
        p = Path(raw)
        if p.exists():
            return p
    return Path(candidates[1])


def _aggregate_futures_daily(fut: pd.DataFrame) -> pd.DataFrame:
    """One row per trade_date with session last close."""
    if fut.empty:
        return pd.DataFrame(columns=["trade_date", "close"])
    df = fut.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    if "close" not in df.columns:
        raise ValueError("futures frame missing 'close' column")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    daily = (
        df.sort_values("timestamp" if "timestamp" in df.columns else "trade_date")
        .groupby("trade_date", as_index=False)["close"]
        .last()
    )
    return daily


def load_futures_daily_closes(parquet_root: Path) -> pd.DataFrame:
    """Load all futures daily closes from parquet_data/futures/."""
    futures_root = parquet_root / "futures"
    if not futures_root.exists():
        return pd.DataFrame(columns=["trade_date", "close"])

    try:
        from snapshot_app.historical.parquet_store import ParquetStore

        store = ParquetStore(parquet_root)
        try:
            days = store.available_days()
            if not days:
                return pd.DataFrame(columns=["trade_date", "close"])
            fut = store.futures_window_for_days(days)
            return _aggregate_futures_daily(fut)
        finally:
            store.close()
    except Exception:
        pass

    frames: list[pd.DataFrame] = []
    for path in sorted(futures_root.rglob("*.parquet")):
        try:
            chunk = pd.read_parquet(path, columns=["timestamp", "trade_date", "close"])
        except Exception:
            chunk = pd.read_parquet(path)
            if "close" not in chunk.columns:
                continue
        frames.append(chunk)
    if not frames:
        return pd.DataFrame(columns=["trade_date", "close"])
    return _aggregate_futures_daily(pd.concat(frames, ignore_index=True))


def load_daily_closes_from_flat_v3(flat_v3_root: Path) -> pd.DataFrame:
    """Fallback: last px_fut_close per trade_date from existing flat v3 files."""
    frames: list[pd.DataFrame] = []
    for path in sorted(flat_v3_root.glob("year=*/*.parquet")):
        try:
            df = pd.read_parquet(path, columns=["trade_date", "px_fut_close"])
        except Exception:
            continue
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
        df["close"] = pd.to_numeric(df["px_fut_close"], errors="coerce")
        daily = df.groupby("trade_date", as_index=False)["close"].last()
        frames.append(daily)
    if not frames:
        return pd.DataFrame(columns=["trade_date", "close"])
    out = pd.concat(frames, ignore_index=True)
    return out.groupby("trade_date", as_index=False)["close"].last()


def compute_regime_features(daily: pd.DataFrame) -> pd.DataFrame:
    """Build regime columns; row T uses data through T-1 only."""
    if daily.empty:
        return pd.DataFrame(columns=["trade_date"] + REGIME_COLUMNS)

    df = daily.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df = df.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    if df.empty:
        return pd.DataFrame(columns=["trade_date"] + REGIME_COLUMNS)

    df["ret"] = df["close"].pct_change()
    df["regime_rv20"] = df["ret"].rolling(RV20_MIN_PERIODS, min_periods=RV20_MIN_PERIODS).std() * np.sqrt(
        TRADING_DAYS_PER_YEAR
    )
    sma20 = df["close"].rolling(SMA20_MIN_PERIODS, min_periods=SMA20_MIN_PERIODS).mean()
    df["regime_dist_sma20"] = (df["close"] - sma20) / sma20.replace(0, np.nan)
    df["regime_sma20_slope"] = sma20.pct_change(SMA20_SLOPE_LAG)
    df["regime_60d_return"] = df["close"] / df["close"].shift(RETURN_60D_LAG) - 1.0

    for col in REGIME_COLUMNS:
        df[col] = df[col].shift(1)

    return df[["trade_date"] + REGIME_COLUMNS]


def load_vix_daily(parquet_root: Path) -> pd.DataFrame:
    """Load India VIX daily closes from parquet_data/vix/vix.parquet."""
    path = parquet_root / "vix" / "vix.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["trade_date", "vix_close"])
    df = pd.read_parquet(path, columns=["trade_date", "vix_close"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df["vix_close"] = pd.to_numeric(df["vix_close"], errors="coerce")
    return df.dropna(subset=["vix_close"]).drop_duplicates("trade_date", keep="last")


def compute_vix_regime_features(vix_daily: pd.DataFrame) -> pd.DataFrame:
    """Prior-day VIX close and high-VIX flag for each trade_date (shift 1)."""
    if vix_daily.empty:
        return pd.DataFrame(columns=["trade_date"] + VIX_REGIME_COLUMNS)

    vix = vix_daily.copy()
    vix["trade_date"] = pd.to_datetime(vix["trade_date"]).dt.normalize()
    vix = vix.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    vix["regime_vix_close"] = vix["vix_close"].shift(1)
    vix["regime_vix_high"] = (vix["regime_vix_close"] >= VIX_HIGH_THRESHOLD).astype(float)
    return vix[["trade_date"] + VIX_REGIME_COLUMNS]


def merge_regime_tables(price_regime: pd.DataFrame, vix_regime: pd.DataFrame) -> pd.DataFrame:
    """Outer-merge price- and VIX-derived regime columns on trade_date."""
    if price_regime.empty and vix_regime.empty:
        return pd.DataFrame(columns=["trade_date"] + ALL_REGIME_COLUMNS)
    if price_regime.empty:
        return vix_regime
    if vix_regime.empty:
        return price_regime
    return price_regime.merge(vix_regime, on="trade_date", how="outer")


def build_full_regime_table(
    daily_closes: pd.DataFrame,
    *,
    parquet_root: Optional[Path] = None,
    vix_daily: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Price regime + optional VIX regime (loads vix.parquet when parquet_root set)."""
    price = compute_regime_features(daily_closes)
    if vix_daily is not None:
        vix = compute_vix_regime_features(vix_daily)
    elif parquet_root is not None:
        vix = compute_vix_regime_features(load_vix_daily(parquet_root))
    else:
        vix = pd.DataFrame(columns=["trade_date"] + VIX_REGIME_COLUMNS)
    return merge_regime_tables(price, vix)


def regime_table_for_dates(
    daily_closes: pd.DataFrame,
    trade_dates: Optional[Sequence[pd.Timestamp]] = None,
    *,
    parquet_root: Optional[Path] = None,
) -> pd.DataFrame:
    """Full regime table, optionally filtered to trade_dates."""
    table = build_full_regime_table(daily_closes, parquet_root=parquet_root)
    if trade_dates is None or table.empty:
        return table
    want = pd.to_datetime(pd.Series(list(trade_dates))).dt.normalize().unique()
    return table[table["trade_date"].isin(want)].copy()
