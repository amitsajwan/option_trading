"""Build (or refresh) brain daily_regime_features.json.

Run manually or via cron to keep the brain's regime features current.
Combines:
  1. Existing parquet data  (2020 – Oct 2024, already on VM)
  2. Kite historical API    (Nov 2024 – today, uses stored credentials)

Usage
-----
  # Full rebuild (first run or after gap)
  python ops/gcp/build_brain_features.py

  # Refresh only (normal nightly run — only fetches missing dates)
  python ops/gcp/build_brain_features.py --refresh

  # Override paths
  python ops/gcp/build_brain_features.py \
      --parquet-root /opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat \
      --credentials /opt/option_trading/ingestion_app/credentials.json \
      --output /opt/option_trading/.data/ml_pipeline/daily_regime_features.json

Permanent setup (run once):
  python ops/gcp/build_brain_features.py --install-cron
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_PARQUET_ROOT = _REPO_ROOT / ".data/ml_pipeline/parquet_data/snapshots_ml_flat"
_DEFAULT_CREDENTIALS = _REPO_ROOT / "ingestion_app/credentials.json"
_DEFAULT_OUTPUT = _REPO_ROOT / ".data/ml_pipeline/daily_regime_features.json"

# BankNifty continuous data source from Kite.
# exchange:tradingsymbol format; we use NSE:NIFTY BANK index since it has no
# monthly rollover and the regime features only need consistent daily closes.
_KITE_EXCHANGE = "NSE"
_KITE_TRADINGSYMBOL = "NIFTY BANK"
_KITE_INSTRUMENT_TOKEN = 260105    # NSE:NIFTY BANK index


# ── Feature computation (mirrors build_daily_regime_features.py) ──────────────

def _compute_features(daily: pd.Series) -> pd.DataFrame:
    """Compute rolling regime features from daily close series.
    All features are shifted by 1 day to avoid look-ahead bias.
    """
    df = pd.DataFrame({"close": daily.sort_index()})
    df.index = pd.to_datetime(df.index)
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    df["regime_rv20"] = df["log_ret"].rolling(20, min_periods=10).std()
    df["sma20"] = df["close"].rolling(20, min_periods=10).mean()
    df["regime_dist_sma20"] = (df["close"] - df["sma20"]) / df["sma20"]
    df["regime_sma20_slope"] = (
        (df["sma20"] - df["sma20"].shift(5)) / df["sma20"].shift(5).abs().clip(lower=1e-9)
    )
    df["regime_60d_return"] = df["close"].pct_change(60)
    for col in ["regime_rv20", "regime_dist_sma20", "regime_sma20_slope", "regime_60d_return"]:
        df[col] = df[col].shift(1)
    return df[["regime_rv20", "regime_dist_sma20", "regime_sma20_slope", "regime_60d_return"]].dropna(
        subset=["regime_rv20"]
    )


# ── Data sources ──────────────────────────────────────────────────────────────

def _load_parquet_closes(parquet_root: Path) -> pd.Series:
    """Read daily futures closes from snapshots_ml_flat parquet."""
    if not parquet_root.exists():
        logger.warning("parquet root not found: %s — skipping", parquet_root)
        return pd.Series(dtype=float)

    files = sorted(parquet_root.rglob("*.parquet"))
    logger.info("parquet: scanning %d files under %s", len(files), parquet_root)
    chunks: list[pd.DataFrame] = []
    for f in files:
        try:
            chunk = pd.read_parquet(f)
            chunks.append(chunk)
        except Exception as exc:
            logger.debug("skip parquet %s: %s", f.name, exc)

    if not chunks:
        logger.warning("no parquet files could be read")
        return pd.Series(dtype=float)

    df = pd.concat(chunks, ignore_index=True)

    date_col = next((c for c in ("date", "snapshot_date", "trade_date") if c in df.columns), None)
    close_col = next((c for c in ("px_fut_close", "fut_close", "futures_close", "close") if c in df.columns), None)
    if not date_col or not close_col:
        logger.warning("parquet missing date/close columns — found: %s", list(df.columns)[:10])
        return pd.Series(dtype=float)

    df[date_col] = pd.to_datetime(df[date_col])
    daily = df.groupby(df[date_col].dt.date)[close_col].last()
    daily.index = pd.to_datetime(daily.index)
    logger.info("parquet: %d daily closes %s → %s", len(daily), daily.index.min().date(), daily.index.max().date())
    return daily.dropna().sort_index()


def _load_kite_closes(
    from_date: date,
    to_date: date,
    credentials_path: Path,
) -> pd.Series:
    """Fetch daily closes from Kite historical API."""
    if not credentials_path.exists():
        logger.warning("credentials not found: %s", credentials_path)
        return pd.Series(dtype=float)

    try:
        creds = json.loads(credentials_path.read_text(encoding="utf-8-sig"))
        api_key = creds.get("api_key", "").strip()
        access_token = creds.get("access_token", "").strip()
        if not api_key or not access_token:
            logger.warning("credentials missing api_key or access_token")
            return pd.Series(dtype=float)
    except Exception as exc:
        logger.warning("could not read credentials: %s", exc)
        return pd.Series(dtype=float)

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        logger.warning("kiteconnect not installed — pip install kiteconnect")
        return pd.Series(dtype=float)

    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        logger.info(
            "kite: fetching %s daily bars %s → %s",
            _KITE_TRADINGSYMBOL, from_date, to_date
        )
        # Kite limits historical requests to ~2000 bars per call; daily = years of data
        data = kite.historical_data(
            instrument_token=_KITE_INSTRUMENT_TOKEN,
            from_date=from_date,
            to_date=to_date,
            interval="day",
        )
        if not data:
            logger.warning("kite returned empty data")
            return pd.Series(dtype=float)

        series = pd.Series(
            {pd.Timestamp(row["date"]): float(row["close"]) for row in data}
        )
        series.index = pd.to_datetime(series.index).normalize()
        logger.info("kite: %d daily closes %s → %s", len(series), series.index.min().date(), series.index.max().date())
        return series.sort_index()

    except Exception as exc:
        logger.warning("kite historical fetch failed: %s", exc)
        return pd.Series(dtype=float)


# ── Build ─────────────────────────────────────────────────────────────────────

def build(
    parquet_root: Path,
    credentials_path: Path,
    output_path: Path,
    refresh_only: bool = False,
) -> bool:
    """Build or refresh the daily_regime_features.json.

    Returns True on success.
    """
    # Load existing features to know the latest date we already have
    existing: dict = {}
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            logger.info("existing features: %d dates, latest=%s", len(existing), max(existing) if existing else "none")
        except Exception as exc:
            logger.warning("could not read existing features: %s — rebuilding", exc)

    latest_existing = max(existing.keys()) if existing else "2000-01-01"

    # ── Step 1: parquet data ─────────────────────────────────────────────────
    parquet_closes = _load_parquet_closes(parquet_root)

    # ── Step 2: Kite data for anything after the parquet ────────────────────
    parquet_end = parquet_closes.index.max().date() if len(parquet_closes) else date(2020, 1, 1)
    kite_from = parquet_end + timedelta(days=1)
    kite_to = date.today()

    if refresh_only:
        # Only fetch what we're missing (from day after latest existing feature)
        kite_from = max(
            kite_from,
            date.fromisoformat(latest_existing) + timedelta(days=1)
        )

    kite_closes = pd.Series(dtype=float)
    if kite_from <= kite_to:
        kite_closes = _load_kite_closes(kite_from, kite_to, credentials_path)
    else:
        logger.info("kite: nothing to fetch (parquet already current)")

    # ── Step 3: merge ────────────────────────────────────────────────────────
    all_closes = pd.concat([parquet_closes, kite_closes]).sort_index()
    all_closes = all_closes[~all_closes.index.duplicated(keep="last")]

    if len(all_closes) < 20:
        logger.error("not enough daily data (%d records) to compute features", len(all_closes))
        return False

    logger.info(
        "combined: %d daily closes %s → %s",
        len(all_closes), all_closes.index.min().date(), all_closes.index.max().date()
    )

    # ── Step 4: compute features ──────────────────────────────────────────────
    features_df = _compute_features(all_closes)
    logger.info("features computed: %d records %s → %s",
                len(features_df), features_df.index.min().date(), features_df.index.max().date())

    # ── Step 5: merge with existing (keep old, overwrite new) ─────────────────
    result: dict = dict(existing)
    for dt, row in features_df.iterrows():
        date_str = pd.Timestamp(dt).date().isoformat()
        result[date_str] = {
            col: (round(float(val), 8) if pd.notna(val) else None)
            for col, val in row.items()
        }

    # ── Step 6: write ─────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(output_path)

    latest = max(result.keys())
    today_str = date.today().isoformat()
    status = "✓ TODAY included" if latest >= today_str else f"⚠ latest={latest} (today={today_str})"
    logger.info("written %d dates to %s  %s", len(result), output_path, status)
    return True


# ── Cron installer ────────────────────────────────────────────────────────────

def install_cron(script_path: Path) -> None:
    """Install a daily 08:45 IST cron job (once per system)."""
    python_bin = sys.executable
    log_path = script_path.parent / "brain_features_build.log"
    cron_line = (
        f"45 3 * * 1-5 {python_bin} {script_path} --refresh "
        f">> {log_path} 2>&1"
    )
    # 08:45 IST = 03:15 UTC, Mon-Fri. Using 03:45 UTC to be safe.
    cron_line = (
        f"45 3 * * 1-5 {python_bin} {script_path} --refresh "
        f">> {log_path} 2>&1"
    )

    try:
        existing = subprocess.check_output(["crontab", "-l"], stderr=subprocess.DEVNULL).decode()
    except subprocess.CalledProcessError:
        existing = ""

    if "build_brain_features.py" in existing:
        print("Cron already installed:")
        for line in existing.splitlines():
            if "build_brain_features" in line:
                print(" ", line)
        return

    new_crontab = existing.rstrip() + f"\n{cron_line}\n"
    proc = subprocess.run(["crontab", "-"], input=new_crontab.encode(), capture_output=True)
    if proc.returncode == 0:
        print(f"✓ Cron installed: {cron_line}")
        print(f"  Log: {log_path}")
    else:
        print(f"✗ crontab failed: {proc.stderr.decode()}")
        print(f"  Add manually: {cron_line}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--parquet-root", type=Path, default=_DEFAULT_PARQUET_ROOT)
    parser.add_argument("--credentials", type=Path, default=_DEFAULT_CREDENTIALS)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--refresh", action="store_true", help="Only fetch missing recent dates (faster)")
    parser.add_argument("--install-cron", action="store_true", help="Install daily 08:45 IST cron job and exit")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.install_cron:
        install_cron(Path(__file__).resolve())
        return 0

    ok = build(
        parquet_root=args.parquet_root,
        credentials_path=args.credentials,
        output_path=args.output,
        refresh_only=args.refresh,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
