#!/usr/bin/env python3
"""SKELETON — 2025 BankNifty data ingestion from Kite Connect Historical API.

This script is intentionally a skeleton. Real fetching needs:
  - Active Kite Connect subscription with Historical API add-on (~₹2000/month)
  - api_key, api_secret, access_token (refreshed daily)
  - Rate-limit handling (Kite: 3 requests/sec/historical endpoint)
  - Symbol resolution (BANKNIFTY futures + monthly+weekly expiries × ATM±N strikes)

Once those are in place, fill in the TODOs below and run:
    python3 ingest_kite_historical.py --from 2025-01-01 --to 2025-12-31

Output: parquet files matching the existing phase1_market_snapshots schema, at:
    /opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2/year=2025/

References:
  - Kite Connect Python SDK: https://github.com/zerodha/pykiteconnect
  - Existing snapshot schema: market_data_dashboard/schemas/ or phase1_market_snapshots_historical in mongo
  - Existing 2024 parquet on the VM for column reference: parquet_data/snapshots_ml_flat_v2/year=2024/

Cost estimate: 1 month Kite Historical API subscription = ~₹2000+GST.
  Lets you pull all of 2025 in batch.

Total data volume estimate:
  - ~250 trading days × 375 bars × 1 futures + ~40 active strikes × 2 (CE/PE) = ~7.5M rows
  - ≈ 1-2 GB parquet, fits comfortably on the runtime VM
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

# TODO: pip install kiteconnect pandas pyarrow
# from kiteconnect import KiteConnect

PARQUET_ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data")
RATE_LIMIT_RPS = 3  # Kite historical endpoint
ATM_STRIKE_WIDTH = 10  # strikes either side of ATM to fetch

log = logging.getLogger(__name__)


@dataclass
class KiteCreds:
    api_key: str
    access_token: str  # generated daily via login flow

    @classmethod
    def from_env(cls) -> "KiteCreds":
        ak = os.environ.get("KITE_API_KEY")
        at = os.environ.get("KITE_ACCESS_TOKEN")
        if not ak or not at:
            sys.exit("set KITE_API_KEY and KITE_ACCESS_TOKEN in env (see kiteconnect docs for daily login flow)")
        return cls(api_key=ak, access_token=at)


def daterange(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # skip weekends; holidays filtered later via NSE calendar
            yield cur
        cur += timedelta(days=1)


def banknifty_futures_symbol_for(d: date) -> str:
    """BANKNIFTY26MARFUT-style symbol depends on contract month.
    For historical fetches, Kite uses the instrument_token from the instruments list, not the symbol directly.
    TODO: use kite.instruments('NFO') to resolve token by (name='BANKNIFTY', segment='NFO-FUT', expiry≈month-end after d)
    """
    raise NotImplementedError("resolve via kite.instruments()")


def banknifty_option_tokens_for(d: date, atm_strike: int) -> List[int]:
    """Active option strikes around ATM for date d, including weekly + monthly expiries.
    TODO:
      - kite.instruments('NFO') filtered to name='BANKNIFTY', segment='NFO-OPT'
      - keep strikes within [atm - ATM_STRIKE_WIDTH*100, atm + ATM_STRIKE_WIDTH*100]
      - keep expiries within next 14 days (weekly + monthly active)
      - return list of instrument_tokens
    """
    raise NotImplementedError("resolve via kite.instruments()")


def fetch_minute_bars(kite, instrument_token: int, day: date) -> List[dict]:
    """Single-day 1-min OHLCV+OI fetch from Kite historical_data endpoint.
    TODO:
        kite.historical_data(
            instrument_token=instrument_token,
            from_date=datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15),
            to_date=datetime.combine(day, datetime.min.time()).replace(hour=15, minute=30),
            interval='minute',
            oi=True,
        )
    Returns list of dicts with keys: date, open, high, low, close, volume, oi
    """
    raise NotImplementedError("kite.historical_data()")


def assemble_snapshot(day: date, fut_bars: List[dict], opt_bars_by_token: dict) -> List[dict]:
    """Stitch futures + option chain into per-minute snapshot rows matching the
    existing phase1_market_snapshots schema.
    TODO: emit one row per minute with:
        - futures_bar.{fut_open, fut_high, fut_low, fut_close, fut_volume, fut_oi}
        - strikes[]: list of {strike, ce_ltp, pe_ltp, ce_oi, pe_oi, ce_iv, pe_iv}
        - chain_aggregates.{atm_strike, total_ce_oi, total_pe_oi, pcr}
        - iv_derived.{iv_percentile, iv_skew, ...} (compute rolling iv_percentile over training set)
        - vix_context (separate Kite fetch for INDIA VIX)
    """
    raise NotImplementedError("schema assembly")


def write_parquet(rows: List[dict], year: int) -> Path:
    """Append rows to /opt/.../snapshots_ml_flat_v2/year=YYYY/part-XX.parquet
    TODO: pyarrow.Table.from_pylist(rows).to_parquet(...)
    """
    raise NotImplementedError("parquet write")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    creds = KiteCreds.from_env()
    # TODO: from kiteconnect import KiteConnect
    # kite = KiteConnect(api_key=creds.api_key)
    # kite.set_access_token(creds.access_token)
    kite = None  # placeholder

    days = list(daterange(start, end))
    log.info("Ingestion plan: %d trading days, %s → %s, rate=%d req/s",
             len(days), start, end, RATE_LIMIT_RPS)
    if args.dry_run:
        print(f"Would fetch {len(days)} days × (1 fut + ~{ATM_STRIKE_WIDTH*2*2} options) instruments.")
        print(f"Estimated requests: {len(days) * (1 + ATM_STRIKE_WIDTH*2*2)}")
        print(f"At {RATE_LIMIT_RPS} req/s: ~{len(days) * (1 + ATM_STRIKE_WIDTH*2*2) / RATE_LIMIT_RPS / 60:.0f} minutes wallclock")
        return

    for day in days:
        # TODO: resolve ATM strike from prior-day futures close
        # TODO: resolve fut + option tokens for day
        # TODO: fetch_minute_bars for each token (respect rate limit: time.sleep(1/RATE_LIMIT_RPS))
        # TODO: assemble_snapshot rows
        # TODO: write_parquet
        log.info("TODO: fetch %s", day)
        time.sleep(0.1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
