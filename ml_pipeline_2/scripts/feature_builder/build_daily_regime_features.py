"""Build daily rolling regime features for TradingBrain.

Reads the futures price parquet (or snapshots_ml_flat) and produces
``daily_regime_features.json`` — one record per trading day with the
features needed by DailyFeaturesProvider.

Output features per day
-----------------------
regime_rv20        trailing 20-day realised volatility (std of daily log returns)
regime_dist_sma20  (close - SMA20) / SMA20  (spot distance from 20-day MA)
regime_sma20_slope 5-day change of SMA20 / SMA20  (trend slope, normalised)
regime_60d_return  cumulative futures return over trailing 60 calendar days

All values are computed as of the *previous* trading day (i.e. available
before the session opens).  Today's value is never used to avoid look-ahead.

Usage
-----
Run nightly (e.g. as a cron at 08:45 IST) before strategy_app starts:

    python -m ml_pipeline_2.scripts.feature_builder.build_daily_regime_features \\
        --parquet-root /opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v3 \\
        --output /opt/option_trading/.run/strategy_app/daily_regime_features.json

Or with a dedicated futures CSV / parquet (faster):

    python -m ml_pipeline_2.scripts.feature_builder.build_daily_regime_features \\
        --futures-csv /opt/option_trading/.data/banknifty_futures_daily.csv \\
        --output /opt/option_trading/.run/strategy_app/daily_regime_features.json

Parquet mode
------------
Scans all date-partitioned parquet files, takes the LAST bar of each day
(15:29 IST close), and builds a daily close series.  Slower but requires no
separate data source.

CSV mode
--------
Expects a CSV with columns: date (YYYY-MM-DD), close (futures close).
Much faster.

Output format
-------------
JSON dict keyed by ISO date::

    {
      "2024-05-15": {
        "regime_rv20": 0.0112,
        "regime_dist_sma20": 0.0043,
        "regime_sma20_slope": 0.00018,
        "regime_60d_return": 0.0674
      },
      ...
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ─────────────────────────── Rolling-feature computation ────────────────────

def _compute_daily_features(daily: pd.Series) -> pd.DataFrame:
    """Given a DatetimeIndex Series of daily closes, return a DataFrame of
    regime features.  All features are shifted by 1 day (previous close)
    to prevent look-ahead bias.

    Parameters
    ----------
    daily : pd.Series
        Index = pd.DatetimeIndex (or date strings), values = futures close prices.
    """
    df = pd.DataFrame({"close": daily.sort_index()})
    df.index = pd.to_datetime(df.index)

    # Log returns
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))

    # Trailing 20-day realised vol (annualised: × sqrt(252), but kept as daily
    # fraction here so thresholds in DailyFeaturesProvider work directly)
    df["regime_rv20"] = df["log_ret"].rolling(20, min_periods=10).std()

    # SMA-20
    df["sma20"] = df["close"].rolling(20, min_periods=10).mean()

    # Distance from SMA-20: (close - SMA20) / SMA20
    df["regime_dist_sma20"] = (df["close"] - df["sma20"]) / df["sma20"]

    # SMA-20 slope: (SMA20 today - SMA20 5 days ago) / SMA20 5 days ago
    df["regime_sma20_slope"] = (
        df["sma20"] - df["sma20"].shift(5)
    ) / df["sma20"].shift(5).abs().clip(lower=1e-9)

    # 60-day cumulative futures return
    df["regime_60d_return"] = df["close"].pct_change(60)

    # Shift by 1: today's features should reflect *yesterday's* data
    feature_cols = ["regime_rv20", "regime_dist_sma20", "regime_sma20_slope", "regime_60d_return"]
    for col in feature_cols:
        df[col] = df[col].shift(1)

    df = df.dropna(subset=["regime_rv20"])
    return df[feature_cols]


# ─────────────────────────── Data loading ───────────────────────────────────

def _load_from_csv(csv_path: Path) -> pd.Series:
    df = pd.read_csv(csv_path, parse_dates=["date"])
    df = df.set_index("date").sort_index()
    if "close" not in df.columns:
        # Try common alternatives
        for col in ("fut_close", "futures_close", "Close", "Adj Close", "last"):
            if col in df.columns:
                df = df.rename(columns={col: "close"})
                break
    if "close" not in df.columns:
        raise ValueError(
            f"CSV {csv_path} must have a 'close' (or 'fut_close') column. "
            f"Found: {list(df.columns)}"
        )
    return df["close"].dropna()


_CLOSE_COL_CANDIDATES = ("px_fut_close", "fut_close", "futures_close", "close")


def _resolve_close_col(df: pd.DataFrame, close_col: str) -> str:
    if close_col in df.columns:
        return close_col
    for candidate in _CLOSE_COL_CANDIDATES:
        if candidate in df.columns:
            logger.info("using close column %s (requested %s not found)", candidate, close_col)
            return candidate
    raise ValueError(
        f"Close column '{close_col}' not found. Available: {list(df.columns)}"
    )


def _load_from_parquet_root(parquet_root: Path, close_col: str = "px_fut_close") -> pd.Series:
    """Scan date-partitioned parquet files and extract daily closes.

    Expects directory structure::

        parquet_root/
            date=2020-08-03/
                part-0.parquet
            date=2020-08-04/
                ...

    Or flat parquet files with a 'date' or 'snapshot_date' column.
    """
    files = sorted(parquet_root.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"No parquet files found under {parquet_root}")

    logger.info("loading %d parquet files from %s", len(files), parquet_root)
    chunks: list[pd.DataFrame] = []
    for fpath in files:
        try:
            chunk = pd.read_parquet(fpath, columns=_parquet_needed_cols(fpath, close_col))
            chunks.append(chunk)
        except Exception as exc:
            logger.warning("skip parquet %s error=%s", fpath.name, exc)

    if not chunks:
        raise ValueError("No parquet files could be read")

    df = pd.concat(chunks, ignore_index=True)

    # Determine date column
    date_col = None
    for candidate in ("date", "snapshot_date", "trade_date"):
        if candidate in df.columns:
            date_col = candidate
            break
    if date_col is None and df.index.name in ("date", "snapshot_date"):
        df = df.reset_index()
        date_col = df.columns[0]

    if date_col is None:
        raise ValueError(
            "Cannot find a date column in parquet. "
            "Expected one of: date, snapshot_date, trade_date"
        )

    close_col = _resolve_close_col(df, close_col)

    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col)

    # Take last bar of each day as the daily close
    daily_close = (
        df.groupby(df[date_col].dt.date)[close_col]
        .last()
        .rename_axis("date")
    )
    daily_close.index = pd.to_datetime(daily_close.index)
    return daily_close.dropna()


def _parquet_needed_cols(fpath: Path, close_col: str) -> Optional[list[str]]:
    """Return minimal column list to read; None = read all."""
    try:
        import pyarrow.parquet as pq
        schema = pq.read_schema(fpath)
        available = set(schema.names)
        needed = [close_col]
        for cand in ("date", "snapshot_date", "trade_date"):
            if cand in available:
                needed.append(cand)
                break
        return needed if all(c in available for c in needed) else None
    except Exception:
        return None


# ─────────────────────────── Output ─────────────────────────────────────────

def _write_json(features: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result: dict = {}
    for dt, row in features.iterrows():
        date_str = pd.Timestamp(dt).date().isoformat()
        result[date_str] = {
            col: (round(float(val), 8) if pd.notna(val) else None)
            for col, val in row.items()
        }
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info(
        "daily_regime_features written records=%d path=%s",
        len(result),
        output_path,
    )


# ─────────────────────────── CLI ────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build daily rolling regime features for TradingBrain.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--parquet-root",
        type=Path,
        metavar="DIR",
        help="Root directory of snapshots_ml_flat parquet (date-partitioned).",
    )
    src.add_argument(
        "--futures-csv",
        type=Path,
        metavar="FILE",
        help="CSV file with columns: date, close (daily futures closes).",
    )
    parser.add_argument(
        "--close-col",
        default="px_fut_close",
        help="Futures close column in parquet (default: px_fut_close; falls back to fut_close).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".run/strategy_app/daily_regime_features.json"),
        help="Output JSON path (default: .run/strategy_app/daily_regime_features.json).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        if args.futures_csv:
            logger.info("loading daily closes from CSV: %s", args.futures_csv)
            daily = _load_from_csv(args.futures_csv)
        else:
            logger.info("loading daily closes from parquet root: %s", args.parquet_root)
            daily = _load_from_parquet_root(args.parquet_root, close_col=args.close_col)

        logger.info(
            "daily closes loaded: %d records %s → %s",
            len(daily),
            daily.index.min().date(),
            daily.index.max().date(),
        )
        features = _compute_daily_features(daily)
        logger.info("regime features computed: %d records", len(features))
        _write_json(features, args.output)
        return 0
    except Exception as exc:
        logger.error("build_daily_regime_features failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
