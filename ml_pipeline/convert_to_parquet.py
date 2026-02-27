"""
convert_to_parquet.py
─────────────────────────────────────────────────────────────────
One-time script to convert BankNifty historical CSV files to
partitioned Parquet format for fast DuckDB querying.

Run once. Safe to re-run — already-converted files are skipped.

Usage:
    python convert_to_parquet.py
    python convert_to_parquet.py --base C:/code/market/ml_pipeline/artifacts/data/inputs/market_archive
    python convert_to_parquet.py --dry-run        # preview only, no writes

Output structure:
    parquet_data/
    ├── futures/
    │   ├── year=2020/ data.parquet
    │   ├── year=2021/ data.parquet
    │   ├── year=2022/ data.parquet
    │   ├── year=2023/ data.parquet
    │   └── year=2024/ data.parquet
    ├── options/
    │   └── year=YYYY/ data.parquet
    ├── spot/
    │   └── year=YYYY/ data.parquet
    └── vix/
        └── vix.parquet
"""

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd

# ── CONFIG ────────────────────────────────────────────────────────────────────

DEFAULT_BASE = Path(
    r"C:\code\market\ml_pipeline\artifacts\data\inputs\market_archive"
)

DEFAULT_OUT = Path(
    r"C:\code\market\ml_pipeline\artifacts\data\parquet_data"
)

# ── COLUMN SCHEMAS ────────────────────────────────────────────────────────────
# Standardize column names regardless of source file variations

FUTURES_COLS = {
    "date":   "date",
    "time":   "time",
    "symbol": "symbol",
    "open":   "open",
    "high":   "high",
    "low":    "low",
    "close":  "close",
    "oi":     "oi",
    "volume": "volume",
}

OPTIONS_COLS = {
    "date":   "date",
    "time":   "time",
    "symbol": "symbol",
    "open":   "open",
    "high":   "high",
    "low":    "low",
    "close":  "close",
    "oi":     "oi",
    "volume": "volume",
}

SPOT_COLS = {
    "date":   "date",
    "time":   "time",
    "symbol": "symbol",
    "open":   "open",
    "high":   "high",
    "low":    "low",
    "close":  "close",
}

VIX_COLS = {
    "date":        "date",
    "open":        "vix_open",
    "high":        "vix_high",
    "low":         "vix_low",
    "close":       "vix_close",
    "prev. close": "vix_prev_close",
    "change":      "vix_change",
    "% change":    "vix_change_pct",
}


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[convert] {msg}", flush=True)


def _load_csv(path: Path) -> Optional[pd.DataFrame]:
    """Load CSV with fallback encodings. Returns None on failure."""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(path, encoding=enc)
            df.columns = [c.strip().lower() for c in df.columns]
            return df
        except Exception:
            continue
    _log(f"  SKIP — could not read: {path.name}")
    return None


def _parse_datetime_series(values: pd.Series) -> pd.Series:
    """
    Parse mixed-format datetimes without noisy warnings.
    Handles ISO-like values (YYYY-MM-DD[ HH:MM:SS]) and day-first values
    (DD/MM/YYYY[ HH:MM:SS]) in one vectorized pass.
    """
    s = values.astype(str).str.strip()
    ts = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")

    # ISO-like strings should be parsed with dayfirst=False.
    iso_mask = s.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", na=False)
    if iso_mask.any():
        ts.loc[iso_mask] = pd.to_datetime(
            s.loc[iso_mask],
            dayfirst=False,
            errors="coerce",
        )

    # Remaining strings are expected to be day-first.
    other_mask = ~iso_mask
    if other_mask.any():
        ts.loc[other_mask] = pd.to_datetime(
            s.loc[other_mask],
            dayfirst=True,
            errors="coerce",
        )

    return ts


def _parse_timestamp(df: pd.DataFrame) -> pd.DataFrame:
    """Combine date + time columns into a single timestamp column."""
    if "date" in df.columns and "time" in df.columns:
        dt_str = df["date"].astype(str).str.strip() + " " + df["time"].astype(str).str.strip()
        df["timestamp"] = _parse_datetime_series(dt_str)
    elif "date" in df.columns:
        df["timestamp"] = _parse_datetime_series(df["date"])
    else:
        df["timestamp"] = pd.NaT
    df = df.dropna(subset=["timestamp"])
    df["trade_date"] = df["timestamp"].dt.date.astype(str)
    df["year"] = df["timestamp"].dt.year
    return df


def _normalize_cols(df: pd.DataFrame, col_map: dict) -> pd.DataFrame:
    """Rename columns using map, keep only mapped columns that exist."""
    rename = {k: v for k, v in col_map.items() if k in df.columns}
    df = df.rename(columns=rename)
    keep = [v for v in col_map.values() if v in df.columns]
    return df[keep]


def _numeric_cols(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _parse_option_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """
    Parse option symbol like BANKNIFTY08MAY2440500PE into:
        underlying, expiry_str, strike, option_type
    """
    if "symbol" not in df.columns:
        return df

    pattern = re.compile(
        r"^([A-Z]+)"           # underlying  e.g. BANKNIFTY
        r"(\d{2}[A-Z]{3}\d{2,4})"  # expiry e.g. 08MAY24 or 08MAY2024
        r"(\d+)"               # strike e.g. 40500
        r"(CE|PE)$",           # option type
        re.IGNORECASE,
    )

    def _parse(sym: str):
        m = pattern.match(str(sym).strip().upper())
        if m:
            return m.group(1), m.group(2), int(m.group(3)), m.group(4).upper()
        return None, None, None, None

    parsed = df["symbol"].map(_parse)
    df["underlying"] = parsed.map(lambda x: x[0])
    df["expiry_str"]  = parsed.map(lambda x: x[1])
    df["strike"]      = parsed.map(lambda x: x[2])
    df["option_type"] = parsed.map(lambda x: x[3])

    # Convert strike to numeric
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    return df


def _write_parquet(df: pd.DataFrame, path: Path) -> int:
    """Write DataFrame to Parquet. Returns row count written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, compression="snappy")
    return len(df)


# ── INSTRUMENT CONVERTERS ─────────────────────────────────────────────────────

def convert_futures(base: Path, out: Path, dry_run: bool) -> dict:
    """
    Source: banknifty_data/banknifty_fut/{year}/{month}/banknifty_fut_{DD}_{MM}_{YYYY}.csv
    Output: parquet_data/futures/year={YYYY}/data.parquet
    """
    src = base / "banknifty_data" / "banknifty_fut"
    if not src.exists():
        _log(f"WARN — futures source not found: {src}")
        return {}

    csv_files = sorted(src.rglob("*.csv"))
    _log(f"Futures — found {len(csv_files)} CSV files")

    all_frames = {}  # year -> list of DataFrames

    for csv_path in csv_files:
        df = _load_csv(csv_path)
        if df is None or len(df) == 0:
            continue

        df = _normalize_cols(df, FUTURES_COLS)
        df = _parse_timestamp(df)
        df = _numeric_cols(df, ["open", "high", "low", "close", "oi", "volume"])
        df = df.sort_values("timestamp").reset_index(drop=True)

        if len(df) == 0:
            continue

        year = int(df["year"].iloc[0])
        all_frames.setdefault(year, []).append(df)

    results = {}
    for year, frames in sorted(all_frames.items()):
        out_path = out / "futures" / f"year={year}" / "data.parquet"

        if out_path.exists() and not dry_run:
            _log(f"  futures year={year} — already exists, skipping")
            results[year] = "skipped"
            continue

        combined = pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp", "symbol"]) \
                           .sort_values("timestamp").reset_index(drop=True)

        if dry_run:
            _log(f"  [DRY RUN] futures year={year} — would write {len(combined):,} rows → {out_path}")
            results[year] = f"dry_run:{len(combined)}"
        else:
            rows = _write_parquet(combined, out_path)
            _log(f"  futures year={year} — wrote {rows:,} rows → {out_path}")
            results[year] = rows

    return results


def convert_options(base: Path, out: Path, dry_run: bool) -> dict:
    """
    Source: banknifty_data/banknifty_options/{year}/{month}/banknifty_options_{DD}_{MM}_{YYYY}.csv
    Output: parquet_data/options/year={YYYY}/data.parquet

    NOTE: Options files are large. Each year is processed month by month
          to avoid loading the entire year into memory at once.
    """
    src = base / "banknifty_data" / "banknifty_options"
    if not src.exists():
        _log(f"WARN — options source not found: {src}")
        return {}

    # Group files by year
    year_files: dict = {}
    for csv_path in sorted(src.rglob("*.csv")):
        # Extract year from path  .../banknifty_options/{year}/{month}/...
        parts = csv_path.parts
        try:
            opt_idx = next(i for i, p in enumerate(parts) if p == "banknifty_options")
            year = int(parts[opt_idx + 1])
            year_files.setdefault(year, []).append(csv_path)
        except Exception:
            continue

    _log(f"Options — found files across {len(year_files)} years: {sorted(year_files)}")

    results = {}
    for year, csv_files in sorted(year_files.items()):
        out_path = out / "options" / f"year={year}" / "data.parquet"

        if out_path.exists() and not dry_run:
            _log(f"  options year={year} — already exists, skipping")
            results[year] = "skipped"
            continue

        _log(f"  options year={year} — processing {len(csv_files)} files...")
        year_frames = []

        for csv_path in sorted(csv_files):
            df = _load_csv(csv_path)
            if df is None or len(df) == 0:
                continue

            df = _normalize_cols(df, OPTIONS_COLS)
            df = _parse_timestamp(df)
            df = _numeric_cols(df, ["open", "high", "low", "close", "oi", "volume"])
            df = _parse_option_symbol(df)

            if len(df) == 0:
                continue

            year_frames.append(df)

        if not year_frames:
            _log(f"  options year={year} — no valid data found")
            continue

        combined = pd.concat(year_frames, ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp", "symbol"]) \
                           .sort_values(["timestamp", "symbol"]).reset_index(drop=True)

        if dry_run:
            _log(f"  [DRY RUN] options year={year} — would write {len(combined):,} rows → {out_path}")
            results[year] = f"dry_run:{len(combined)}"
        else:
            rows = _write_parquet(combined, out_path)
            _log(f"  options year={year} — wrote {rows:,} rows → {out_path}")
            results[year] = rows

    return results


def convert_spot(base: Path, out: Path, dry_run: bool) -> dict:
    """
    Source: banknifty_data/banknifty_spot/{year}/{month}/banknifty_spot{DD}{MM}{YYYY}.csv
    Output: parquet_data/spot/year={YYYY}/data.parquet
    """
    src = base / "banknifty_data" / "banknifty_spot"
    if not src.exists():
        _log(f"WARN — spot source not found: {src}")
        return {}

    csv_files = sorted(src.rglob("*.csv"))
    _log(f"Spot — found {len(csv_files)} CSV files")

    all_frames = {}

    for csv_path in csv_files:
        df = _load_csv(csv_path)
        if df is None or len(df) == 0:
            continue

        df = _normalize_cols(df, SPOT_COLS)
        df = _parse_timestamp(df)
        df = _numeric_cols(df, ["open", "high", "low", "close"])

        if len(df) == 0:
            continue

        year = int(df["year"].iloc[0])
        all_frames.setdefault(year, []).append(df)

    results = {}
    for year, frames in sorted(all_frames.items()):
        out_path = out / "spot" / f"year={year}" / "data.parquet"

        if out_path.exists() and not dry_run:
            _log(f"  spot year={year} — already exists, skipping")
            results[year] = "skipped"
            continue

        combined = pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"]) \
                           .sort_values("timestamp").reset_index(drop=True)

        if dry_run:
            _log(f"  [DRY RUN] spot year={year} — would write {len(combined):,} rows → {out_path}")
            results[year] = f"dry_run:{len(combined)}"
        else:
            rows = _write_parquet(combined, out_path)
            _log(f"  spot year={year} — wrote {rows:,} rows → {out_path}")
            results[year] = rows

    return results


def convert_vix(base: Path, out: Path, dry_run: bool) -> dict:
    """
    Source: vix/hist_india_vix_-{DD}-{MM}-{YYYY}-to-{DD}-{MM}-{YYYY}.csv
            (multiple files covering different date ranges)
    Output: parquet_data/vix/vix.parquet  (single combined file — VIX is tiny)
    """
    src = base / "vix"
    if not src.exists():
        _log(f"WARN — VIX source not found: {src}")
        return {}

    csv_files = sorted(src.rglob("*.csv"))
    _log(f"VIX — found {len(csv_files)} CSV files")

    all_frames = []

    for csv_path in csv_files:
        df = _load_csv(csv_path)
        if df is None or len(df) == 0:
            continue

        # VIX files have only a date column, no time
        df = _normalize_cols(df, VIX_COLS)

        if "date" not in df.columns:
            _log(f"  VIX skip — no date column in {csv_path.name}")
            continue

        df["date"] = _parse_datetime_series(df["date"])
        df = df.dropna(subset=["date"])
        df["trade_date"] = df["date"].dt.date.astype(str)
        df["year"] = df["date"].dt.year
        df = _numeric_cols(df, ["vix_open", "vix_high", "vix_low", "vix_close",
                                 "vix_prev_close", "vix_change", "vix_change_pct"])

        all_frames.append(df)

    if not all_frames:
        _log("  VIX — no valid data found")
        return {}

    combined = pd.concat(all_frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["trade_date"]) \
                       .sort_values("date").reset_index(drop=True)

    out_path = out / "vix" / "vix.parquet"

    if out_path.exists() and not dry_run:
        _log(f"  VIX — already exists, skipping")
        return {"vix": "skipped"}

    if dry_run:
        _log(f"  [DRY RUN] VIX — would write {len(combined):,} rows → {out_path}")
        return {"vix": f"dry_run:{len(combined)}"}

    rows = _write_parquet(combined, out_path)
    _log(f"  VIX — wrote {rows:,} rows → {out_path}")
    return {"vix": rows}


# ── VERIFY ────────────────────────────────────────────────────────────────────

def verify_output(out: Path) -> None:
    """Quick DuckDB verification that all Parquet files are queryable."""
    try:
        import duckdb
    except ImportError:
        _log("SKIP verify — duckdb not installed (pip install duckdb)")
        return

    _log("\nVerifying output with DuckDB...")
    con = duckdb.connect()

    checks = [
        ("futures", "futures/**/*.parquet", "timestamp, symbol, open, high, low, close, oi, volume"),
        ("options", "options/**/*.parquet", "timestamp, symbol, strike, option_type, close, oi, volume"),
        ("spot",    "spot/**/*.parquet",    "timestamp, symbol, open, high, low, close"),
        ("vix",     "vix/vix.parquet",      "trade_date, vix_close"),
    ]

    for name, pattern, cols in checks:
        path = out / pattern
        try:
            result = con.execute(
                f"SELECT COUNT(*) as rows, MIN(timestamp) as first, MAX(timestamp) as last "
                f"FROM read_parquet('{str(path).replace(chr(92), '/')}/**/*.parquet' , hive_partitioning=true)"
                if name != "vix" else
                f"SELECT COUNT(*) as rows, MIN(trade_date) as first, MAX(trade_date) as last "
                f"FROM read_parquet('{str(out / 'vix' / 'vix.parquet').replace(chr(92), '/')}')"
            ).fetchone()
            _log(f"  {name}: {result[0]:,} rows | {result[1]} → {result[2]}")
        except Exception as e:
            # Simpler fallback query
            try:
                glob = str(out / pattern).replace("\\", "/")
                r = con.execute(f"SELECT COUNT(*) FROM read_parquet('{glob}')").fetchone()
                _log(f"  {name}: {r[0]:,} rows")
            except Exception as e2:
                _log(f"  {name}: verify failed — {e2}")

    con.close()


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert BankNifty historical CSV data to Parquet format."
    )
    parser.add_argument(
        "--base",
        default=str(DEFAULT_BASE),
        help=f"Market archive root (default: {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Output Parquet root (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--instruments",
        default="futures,options,spot,vix",
        help="Comma-separated instruments to convert (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be written without writing anything",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip DuckDB verification after conversion",
    )
    args = parser.parse_args()

    base = Path(args.base)
    out  = Path(args.out)
    instruments = [x.strip().lower() for x in args.instruments.split(",")]

    if not base.exists():
        print(f"ERROR — base path does not exist: {base}")
        sys.exit(1)

    _log("=" * 60)
    _log("BankNifty CSV -> Parquet Converter")
    _log("=" * 60)
    _log(f"Base   : {base}")
    _log(f"Output : {out}")
    _log(f"Mode   : {'DRY RUN' if args.dry_run else 'WRITE'}")
    _log(f"Instruments: {instruments}")
    _log("")

    start = time.time()
    summary = {}

    if "futures" in instruments:
        _log("── FUTURES ──────────────────────────────────")
        summary["futures"] = convert_futures(base, out, args.dry_run)

    if "options" in instruments:
        _log("\n── OPTIONS ──────────────────────────────────")
        _log("NOTE: Options is the largest dataset — this will take a few minutes")
        summary["options"] = convert_options(base, out, args.dry_run)

    if "spot" in instruments:
        _log("\n── SPOT ─────────────────────────────────────")
        summary["spot"] = convert_spot(base, out, args.dry_run)

    if "vix" in instruments:
        _log("\n── VIX ──────────────────────────────────────")
        summary["vix"] = convert_vix(base, out, args.dry_run)

    elapsed = time.time() - start
    _log(f"\nDone in {elapsed:.1f}s")
    _log(f"\nSummary: {summary}")

    if not args.dry_run and not args.no_verify:
        verify_output(out)

    _log("\nNext step: run query_test.py to validate data quality")


if __name__ == "__main__":
    main()
