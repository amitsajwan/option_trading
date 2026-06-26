"""
Dhan Historical Data Pipeline — 3-step snapshot builder for ML training.

Steps (each saves intermediate output, can be run independently):
  1. fetch   -> raw parquet per instrument per day (raw API response)
  2. build   -> indicators parquet per day (derived features from raw)
  3. assemble-> final snapshot parquet (training-ready, same schema as snapshots_ml_flat_v2)

Each step verifies its own output before completing.

Usage:
  # Full pipeline: BANKNIFTY, 5 years
  python ml_pipeline_2/scripts/dhan_data_pipeline.py fetch \
      --instrument BANKNIFTY --start 2021-06-01 --end 2026-06-25 \
      --token $DHAN_TOKEN --client-id 1111957145 \
      --out-dir .data/dhan_pipeline/raw

  python ml_pipeline_2/scripts/dhan_data_pipeline.py build \
      --raw-dir .data/dhan_pipeline/raw \
      --out-dir .data/dhan_pipeline/indicators

  python ml_pipeline_2/scripts/dhan_data_pipeline.py assemble \
      --indicators-dir .data/dhan_pipeline/indicators \
      --out-dir .data/ml_pipeline/parquet_data/snapshots_dhan_v1

  # Verify a specific step
  python ml_pipeline_2/scripts/dhan_data_pipeline.py verify \
      --stage fetch --raw-dir .data/dhan_pipeline/raw

  # Quick smoke test on 1 week
  python ml_pipeline_2/scripts/dhan_data_pipeline.py fetch \
      --instrument BANKNIFTY --start 2026-06-16 --end 2026-06-20 \
      --token $DHAN_TOKEN --client-id 1111957145 \
      --out-dir /tmp/dhan_smoke --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import warnings

import numpy as np
import pandas as pd
import requests

# Shared feature computation — single source of truth for training AND runtime.
from snapshot_app.core.feature_engine import build_features

# Batch pipeline — suppress fragmentation warning from column-by-column option assembly
warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)

log = logging.getLogger("dhan_pipeline")

# ── Constants ─────────────────────────────────────────────────────────────────

DHAN_BASE = "https://api.dhan.co/v2"
IST = "Asia/Kolkata"
SESSION_START = "09:15"
SESSION_END = "15:30"

# Rolling option ATM strikes to fetch (±5 covers PCR, max_pain, skew)
DEFAULT_STRIKES = (
    ["ATM"]
    + [f"ATM+{i}" for i in range(1, 6)]
    + [f"ATM-{i}" for i in range(1, 6)]
)

# ── Instrument Configuration ──────────────────────────────────────────────────

@dataclass
class InstrumentConfig:
    name: str
    index_security_id: str   # Dhan security ID for the index (IDX_I segment)
    fno_segment: str         # "NSE_FNO" for NSE derivatives
    index_segment: str       # "IDX_I" for NSE indices
    lot_size: int
    strike_step: int         # Minimum strike increment (points)
    # VIX is shared across all instruments
    vix_security_id: str = "21"
    vix_segment: str = "IDX_I"


INSTRUMENTS: Dict[str, InstrumentConfig] = {
    "BANKNIFTY": InstrumentConfig(
        name="BANKNIFTY",
        index_security_id="25",   # Confirmed: ~58,400 in Jun 2026
        fno_segment="NSE_FNO",
        index_segment="IDX_I",
        lot_size=30,
        strike_step=100,
    ),
    "NIFTY": InstrumentConfig(
        name="NIFTY",
        index_security_id="13",   # Confirmed: ~24,000 in Jun 2026
        fno_segment="NSE_FNO",
        index_segment="IDX_I",
        lot_size=75,
        strike_step=50,
    ),
}

# ── Dhan API Client ───────────────────────────────────────────────────────────

class DhanClient:
    """REST client with rate limiting and retry."""

    def __init__(self, token: str, client_id: str, rps: float = 4.0):
        self.token = token
        self.client_id = client_id
        self._interval = 1.0 / rps
        self._last = 0.0
        self._sess = requests.Session()
        self._sess.headers.update({
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json",
        })

    def _throttle(self):
        gap = time.monotonic() - self._last
        if gap < self._interval:
            time.sleep(self._interval - gap)
        self._last = time.monotonic()

    def post(self, path: str, payload: dict, retries: int = 3) -> dict:
        url = f"{DHAN_BASE}{path}"
        for attempt in range(retries):
            self._throttle()
            try:
                r = self._sess.post(url, json=payload, timeout=30)
                if r.status_code == 429:
                    wait = 2 ** attempt * 2
                    log.warning("Rate limited, waiting %ds", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                log.warning("Timeout on %s attempt %d/%d", path, attempt + 1, retries)
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Failed {path} after {retries} attempts")

    def validate_token(self) -> bool:
        """Lightweight token check — returns True if valid."""
        try:
            r = self._sess.get(f"{DHAN_BASE}/profile", timeout=10)
            return r.status_code == 200
        except Exception:
            return False


# ── Step 1: FETCH ─────────────────────────────────────────────────────────────

def fetch_intraday(
    client: DhanClient,
    security_id: str,
    segment: str,
    instrument_type: str,
    start: date,
    end: date,
    interval: int = 1,
) -> pd.DataFrame:
    """Fetch intraday OHLCV in 90-day chunks. Returns unified DataFrame."""
    parts = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=89), end)
        try:
            resp = client.post("/charts/intraday", {
                "securityId": security_id,
                "exchangeSegment": segment,
                "instrument": instrument_type,
                "interval": interval,
                "fromDate": cursor.isoformat(),
                "toDate": chunk_end.isoformat(),
            })
            if resp.get("open"):
                n = len(resp["open"])
                df = pd.DataFrame({
                    "open": resp["open"],
                    "high": resp["high"],
                    "low": resp["low"],
                    "close": resp["close"],
                    "volume": resp.get("volume", [0] * n),
                    "ts": pd.to_datetime(resp["timestamp"], unit="s", utc=True),
                }).set_index("ts")
                parts.append(df)
                log.debug("  intraday %s %s->%s: %d bars", security_id, cursor, chunk_end, n)
            else:
                log.warning("  intraday %s %s->%s: empty response", security_id, cursor, chunk_end)
        except Exception as exc:
            log.error("  intraday %s %s->%s FAILED: %s", security_id, cursor, chunk_end, exc)
        cursor = chunk_end + timedelta(days=1)
    return pd.concat(parts) if parts else pd.DataFrame()


def fetch_rolling_option(
    client: DhanClient,
    cfg: InstrumentConfig,
    strike: str,
    option_type: str,   # "CALL" or "PUT"
    start: date,
    end: date,
    interval: int = 1,
) -> pd.DataFrame:
    """
    Fetch rolling ATM option data for one strike × side.
    Uses 7-day weekly chunks to ensure expiryCode=1 always refers
    to exactly one weekly expiry (no ambiguity across multiple expiries).
    Response key: "ce" for CALL, "pe" for PUT.
    """
    side = "ce" if option_type == "CALL" else "pe"
    parts = []
    cursor = start
    while cursor < end:
        # 7-day chunks = one weekly expiry cycle
        chunk_end = min(cursor + timedelta(days=6), end)
        try:
            resp = client.post("/charts/rollingoption", {
                "securityId": cfg.index_security_id,
                "exchangeSegment": cfg.fno_segment,
                "instrument": "OPTIDX",
                "expiryCode": 1,       # nearest expiry in the date range
                "expiryFlag": "WEEK",  # weekly expiry cycle
                "strike": strike,
                "drvOptionType": option_type,
                "requiredData": ["open", "high", "low", "close", "iv", "oi", "spot", "volume"],
                "fromDate": cursor.isoformat(),
                "toDate": chunk_end.isoformat(),
                "interval": interval,
            })
            data = (resp.get("data") or {}).get(side) or {}
            if data.get("close"):
                n = len(data["close"])
                df = pd.DataFrame({
                    f"{side}_open":   data.get("open",   [None] * n),
                    f"{side}_high":   data.get("high",   [None] * n),
                    f"{side}_low":    data.get("low",    [None] * n),
                    f"{side}_close":  data.get("close",  [None] * n),
                    f"{side}_iv":     data.get("iv",     [None] * n),
                    f"{side}_oi":     data.get("oi",     [None] * n),
                    f"{side}_volume": data.get("volume", [None] * n),
                    "spot":           data.get("spot",   [None] * n),
                    "ts": pd.to_datetime(data["timestamp"], unit="s", utc=True),
                }).set_index("ts")
                parts.append(df)
                log.debug("  rolling %s %s %s->%s: %d bars", strike, side, cursor, chunk_end, n)
            else:
                log.warning("  rolling %s %s %s->%s: empty", strike, side, cursor, chunk_end)
        except Exception as exc:
            log.error("  rolling %s %s %s->%s FAILED: %s", strike, side, cursor, chunk_end, exc)
        cursor = chunk_end + timedelta(days=1)
    return pd.concat(parts) if parts else pd.DataFrame()


def run_fetch(args):
    """Step 1: Download all raw data and save to parquet files."""
    cfg = INSTRUMENTS[args.instrument]
    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    strikes = args.strikes.split(",")

    client = DhanClient(args.token, args.client_id, rps=4.0)

    # Validate token first
    if not client.validate_token():
        log.error("Token validation FAILED — check token and client-id")
        sys.exit(1)
    log.info("Token valid. Fetching %s %s->%s, %d strikes", cfg.name, start, end, len(strikes))

    if args.dry_run:
        log.info("[DRY RUN] would fetch underlying, VIX, %d strikes × 2 sides", len(strikes))
        return

    # ── 1a. Underlying index ──────────────────────────────────────────────────
    log.info("Fetching %s index (securityId=%s)...", cfg.name, cfg.index_security_id)
    index_df = fetch_intraday(client, cfg.index_security_id, cfg.index_segment,
                              "INDEX", start, end, interval=1)
    _save_verify(index_df, raw_dir / "index.parquet", "underlying index",
                 expect_cols=["open", "high", "low", "close", "volume"],
                 price_col="close", price_range=(1000, 200000))

    # ── 1b. VIX ──────────────────────────────────────────────────────────────
    log.info("Fetching India VIX (securityId=%s)...", cfg.vix_security_id)
    vix_df = fetch_intraday(client, cfg.vix_security_id, cfg.vix_segment,
                            "INDEX", start, end, interval=1)
    _save_verify(vix_df, raw_dir / "vix.parquet", "India VIX",
                 expect_cols=["close"],
                 price_col="close", price_range=(5, 100))

    # ── 1c. Rolling options per strike × side ────────────────────────────────
    for strike in strikes:
        for option_type, side in [("CALL", "ce"), ("PUT", "pe")]:
            log.info("Fetching %s %s...", strike, option_type)
            odf = fetch_rolling_option(client, cfg, strike, option_type, start, end)
            fname = f"option_{strike.replace('+','p').replace('-','m')}_{side}.parquet"
            _save_verify(odf, raw_dir / fname, f"{strike} {side}",
                         expect_cols=[f"{side}_close", f"{side}_iv", f"{side}_oi"],
                         price_col=f"{side}_close", price_range=(0.05, 50000))

    log.info("FETCH COMPLETE — raw data in %s", raw_dir)
    _print_fetch_summary(raw_dir, cfg.name, start, end)


def _save_verify(df: pd.DataFrame, path: Path, label: str,
                 expect_cols: list, price_col: str, price_range: Tuple[float, float]):
    """Save parquet and run basic sanity checks."""
    if df.empty:
        log.warning("  WARN: %s — empty DataFrame, saving placeholder", label)
        pd.DataFrame().to_parquet(path)
        return

    missing = [c for c in expect_cols if c not in df.columns]
    if missing:
        log.warning("  WARN: %s — missing columns: %s", label, missing)

    prices = df[price_col].dropna()
    lo, hi = price_range
    out_of_range = ((prices < lo) | (prices > hi)).sum()
    if out_of_range > 0:
        log.warning("  WARN: %s — %d bars with %s outside [%s, %s]",
                    label, out_of_range, price_col, lo, hi)

    nan_pct = df[price_col].isna().mean() * 100
    if nan_pct > 5:
        log.warning("  WARN: %s — %.1f%% NaN in %s", label, nan_pct, price_col)

    df.to_parquet(path)
    log.info("  SAVED: %s -> %s (%d rows, %.1f%% NaN)", label, path.name, len(df), nan_pct)


def _print_fetch_summary(raw_dir: Path, instrument: str, start: date, end: date):
    """Print a summary table of what was fetched."""
    print(f"\n{'='*60}")
    print(f"FETCH SUMMARY: {instrument} {start}->{end}")
    print(f"{'='*60}")
    for f in sorted(raw_dir.glob("*.parquet")):
        try:
            df = pd.read_parquet(f)
            if df.empty:
                print(f"  {f.name:50s} EMPTY")
            else:
                dates = pd.to_datetime(df.index).tz_convert(IST).date
                print(f"  {f.name:50s} {len(df):7,} rows  "
                      f"{dates.min()}->{dates.max()}")
        except Exception as e:
            print(f"  {f.name:50s} ERROR: {e}")
    print()


# ── Technical Indicator Helpers ───────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilders-smoothed ADX (trend strength 0-100)."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    plus_dm = ((high - prev_high).clip(lower=0)
               .where((high - prev_high) > (prev_low - low), 0.0))
    minus_dm = ((prev_low - low).clip(lower=0)
                .where((prev_low - low) > (high - prev_high), 0.0))
    atr14 = _atr(high, low, close, period)
    safe_atr = atr14.replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / safe_atr
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / safe_atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


# ── Monthly expiry calendar (raw Dhan data has no expiry column) ───────────────
# BankNifty is monthly-only post-Nov-2024. Monthly index expiry = last Thursday of
# the month, rolled back to the prior trading day if that Thursday is a holiday.
# (NSE's long-standing monthly-index convention. Off-by-one in a rare sub-period is
# acceptable for v1; documented in plan doc §8a. We pass this into build_features so
# feature_engine's weekly Wed/Thu heuristic is NOT used for the monthly regime.)

import calendar as _calendar


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """weekday: Mon=0 … Sun=6 (Thursday=3)."""
    last_day = _calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _build_monthly_expiry_calendar(trading_days: List[date]) -> Dict[Tuple[int, int], date]:
    trading_set = set(trading_days)
    cal: Dict[Tuple[int, int], date] = {}
    for (y, m) in sorted({(d.year, d.month) for d in trading_days}):
        thu = _last_weekday_of_month(y, m, 3)  # last Thursday
        d = thu
        for _ in range(7):  # roll back to a trading day if holiday
            if d in trading_set:
                break
            d -= timedelta(days=1)
        cal[(y, m)] = d
    return cal


def _monthly_expiry_for(td: date, cal: Dict[Tuple[int, int], date]) -> date:
    exp = cal.get((td.year, td.month))
    if exp is not None and td <= exp:
        return exp
    ny = td.year + 1 if td.month == 12 else td.year
    nm = 1 if td.month == 12 else td.month + 1
    return cal.get((ny, nm)) or _last_weekday_of_month(ny, nm, 3)


# ── Step 2: BUILD INDICATORS ──────────────────────────────────────────────────

def run_build(args):
    """Step 2: Compute all indicators from raw parquet files."""
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Loading raw data from %s", raw_dir)

    # Load all raw parquet
    index_df = _load_parquet(raw_dir / "index.parquet", "underlying index")
    vix_df   = _load_parquet(raw_dir / "vix.parquet",   "VIX")

    # Load all option files — {strike: {"ce": df, "pe": df}}
    options: Dict[str, Dict[str, pd.DataFrame]] = {}
    for strike_label in DEFAULT_STRIKES:
        slug = strike_label.replace("+", "p").replace("-", "m")
        ce_df = _load_parquet(raw_dir / f"option_{slug}_ce.parquet", f"{strike_label} CE")
        pe_df = _load_parquet(raw_dir / f"option_{slug}_pe.parquet", f"{strike_label} PE")
        options[strike_label] = {"ce": ce_df, "pe": pe_df}

    # Get all unique trading dates from the index data
    if index_df.empty:
        log.error("No underlying index data — cannot build indicators")
        sys.exit(1)

    ist_index = index_df.index.tz_convert(IST)
    trade_dates = sorted(set(ist_index.date))

    # Scope to the monthly regime (default 2024-11-01) — see plan doc §8a.
    start_date_str = getattr(args, "start_date", "") or ""
    if start_date_str:
        cutoff = date.fromisoformat(start_date_str)
        full_n = len(trade_dates)
        trade_dates = [td for td in trade_dates if td >= cutoff]
        log.info("Scoped to >= %s: %d of %d trading days (monthly regime)",
                 cutoff.isoformat(), len(trade_dates), full_n)

    # Monthly expiry calendar from the actual trading days (raw data has no expiry column).
    # BankNifty monthly = last Thursday of the month, rolled back to the prior trading day
    # if that Thursday is not a trading day. Built once, passed per day to build_features.
    all_trading_days = sorted(set(ist_index.date))
    expiry_by_month = _build_monthly_expiry_calendar(all_trading_days)

    log.info("Building indicators for %d trading days", len(trade_dates))

    day_files = []
    prev_close: Optional[float] = None
    for td in trade_dates:
        out_file = out_dir / f"{td.isoformat()}.parquet"
        expiry = _monthly_expiry_for(td, expiry_by_month)
        day_df = _build_day_indicators(td, index_df, vix_df, options,
                                       prev_day_close=prev_close, expiry_date=expiry)
        if day_df is not None and not day_df.empty:
            day_df.to_parquet(out_file)
            day_files.append(out_file)
            log.debug("  Built %s: %d bars", td, len(day_df))
            # Track prev_day_close for next day's gap features
            last_close = day_df["px_fut_close"].dropna()
            if len(last_close) > 0:
                prev_close = float(last_close.iloc[-1])

    log.info("BUILD COMPLETE — %d day files in %s", len(day_files), out_dir)
    _verify_indicators(out_dir, trade_dates[:5])  # spot-check first 5 days


def _load_parquet(path: Path, label: str) -> pd.DataFrame:
    if not path.exists():
        log.warning("  MISSING: %s (%s) — continuing with empty", label, path.name)
        return pd.DataFrame()
    df = pd.read_parquet(path)
    log.debug("  Loaded %s: %d rows", label, len(df))
    return df


def _build_day_indicators(
    trade_date: date,
    index_df: pd.DataFrame,
    vix_df: pd.DataFrame,
    options: Dict[str, Dict[str, pd.DataFrame]],
    *,
    prev_day_close: Optional[float] = None,
    expiry_date: Optional[date] = None,
) -> Optional[pd.DataFrame]:
    """Build one day of indicators. Returns None if no index data for that day.

    All features are computable from rolling windows anchored at 9:15 open.
    First tradeable bar is 9:45 (bar 30) — 30 bars of warmup for fast EMAs.
    Column names match snapshots_ml_flat_v2 schema for direct training compat.
    """
    ist_idx = index_df.index.tz_convert(IST)
    day_mask = ist_idx.date == trade_date
    idx_day = index_df[day_mask].copy()
    if idx_day.empty:
        return None

    # ── Session index (1-min from 09:15 to 15:30 IST) ─────────────────────
    day_str = trade_date.isoformat()
    session_open  = pd.Timestamp(f"{day_str} {SESSION_START}", tz=IST)
    session_close = pd.Timestamp(f"{day_str} {SESSION_END}",   tz=IST)
    sess_idx = pd.date_range(session_open, session_close, freq="1min")
    n_bars = len(sess_idx)

    rows = pd.DataFrame(index=sess_idx)
    rows.index.name = "ts_ist"

    # ── Underlying OHLCV (v2 schema names: px_fut_* / px_spot_*) ─────────
    idx_al = idx_day.tz_convert(IST).reindex(sess_idx, method="nearest",
                                              tolerance=pd.Timedelta("90s"))
    for raw, v2 in [("open", "px_fut_open"), ("high", "px_fut_high"),
                    ("low", "px_fut_low"), ("close", "px_fut_close")]:
        rows[v2] = idx_al[raw]
    # Spot = same (we use index as proxy; no basis with Dhan index data)
    for raw, v2 in [("open", "px_spot_open"), ("high", "px_spot_high"),
                    ("low", "px_spot_low"), ("close", "px_spot_close")]:
        rows[v2] = idx_al[raw]
    rows["fut_flow_volume"] = idx_al.get("volume", pd.Series(0, index=sess_idx))

    rows["trade_date"] = trade_date
    rows["instrument"] = "BANKNIFTY"

    # ── VIX (raw column — feature_engine Layer 6 will compute ctx_is_high_vix_day) ─
    vix_open_val: Optional[float] = None
    if not vix_df.empty:
        vix_ist  = vix_df.index.tz_convert(IST)
        vix_mask = vix_ist.date == trade_date
        vix_day  = vix_df[vix_mask].tz_convert(IST)
        if not vix_day.empty:
            vix_al = vix_day.reindex(sess_idx, method="nearest",
                                     tolerance=pd.Timedelta("90s"))
            rows["vix"] = vix_al["close"]
            first_valid = vix_al["close"].dropna()
            if len(first_valid) > 0:
                vix_open_val = float(first_valid.iloc[0])

    # ── Options data ──────────────────────────────────────────────────────
    ce_oi_series, pe_oi_series = [], []
    ce_vol_series, pe_vol_series = [], []

    for strike_label, sides in options.items():
        for side_key, odf in sides.items():
            if odf.empty:
                continue
            odf_ist = odf.index.tz_convert(IST)
            odf_mask = odf_ist.date == trade_date
            odf_day = odf[odf_mask].tz_convert(IST)
            if odf_day.empty:
                continue
            odf_al = odf_day.reindex(sess_idx, method="nearest",
                                     tolerance=pd.Timedelta("90s"))

            is_atm = strike_label == "ATM"
            slug = strike_label.replace("+", "p").replace("-", "m")
            prefix = f"{slug}_{side_key}"

            for col in [f"{side_key}_close", f"{side_key}_iv", f"{side_key}_oi",
                        f"{side_key}_volume", f"{side_key}_open", f"{side_key}_high",
                        f"{side_key}_low"]:
                if col in odf_al.columns:
                    rows[f"{prefix}_{col.split('_', 1)[1]}"] = odf_al[col]

            if is_atm:
                if f"{side_key}_close" in odf_al.columns:
                    rows[f"atm_{side_key}_ltp"]   = odf_al[f"{side_key}_close"]
                    rows[f"atm_{side_key}_close"]  = odf_al[f"{side_key}_close"]
                if f"{side_key}_iv" in odf_al.columns:
                    rows[f"atm_{side_key}_iv"]     = odf_al[f"{side_key}_iv"]
                if f"{side_key}_oi" in odf_al.columns:
                    rows[f"atm_{side_key}_oi"]     = odf_al[f"{side_key}_oi"]
                if f"{side_key}_volume" in odf_al.columns:
                    rows[f"atm_{side_key}_volume"] = odf_al[f"{side_key}_volume"]
                if "spot" in odf_al.columns:
                    rows["spot_from_options"] = odf_al["spot"]

            oi_col  = f"{side_key}_oi"
            vol_col = f"{side_key}_volume"
            if oi_col in odf_al.columns:
                s = odf_al[oi_col].reindex(sess_idx).fillna(0)
                (ce_oi_series if side_key == "ce" else pe_oi_series).append(s)
            if vol_col in odf_al.columns:
                s = odf_al[vol_col].reindex(sess_idx).fillna(0)
                (ce_vol_series if side_key == "ce" else pe_vol_series).append(s)

    # ── Aggregate option-flow features (v2 schema names: opt_flow_*) ─────
    total_ce_oi = pd.Series(0.0, index=sess_idx)
    total_pe_oi = pd.Series(0.0, index=sess_idx)
    if ce_oi_series:
        total_ce_oi = pd.concat(ce_oi_series, axis=1).sum(axis=1)
        rows["opt_flow_ce_oi_total"] = total_ce_oi
    if pe_oi_series:
        total_pe_oi = pd.concat(pe_oi_series, axis=1).sum(axis=1)
        rows["opt_flow_pe_oi_total"] = total_pe_oi
    if ce_vol_series:
        rows["opt_flow_ce_volume_total"] = pd.concat(ce_vol_series, axis=1).sum(axis=1)
    if pe_vol_series:
        rows["opt_flow_pe_volume_total"] = pd.concat(pe_vol_series, axis=1).sum(axis=1)

    if ce_oi_series and pe_oi_series:
        safe_ce = total_ce_oi.replace(0, np.nan)
        rows["opt_flow_pcr_oi"]          = total_pe_oi / safe_ce
        rows["opt_flow_ce_pe_oi_diff"]   = total_ce_oi - total_pe_oi

    if "atm_ce_oi" in rows.columns and "atm_pe_oi" in rows.columns:
        ce_oi = rows["atm_ce_oi"]
        pe_oi = rows["atm_pe_oi"]
        rows["atm_oi_ratio"] = ce_oi / pe_oi.replace(0, np.nan)
        atm_total = ce_oi + pe_oi
        rows["opt_flow_atm_oi_change_1m"] = atm_total.diff(1)
        rows["opt_flow_ce_pe_volume_diff"] = (
            rows.get("atm_ce_volume", 0) - rows.get("atm_pe_volume", 0)
        )
        # Bar-to-bar ATM OI changes (multiple horizons)
        for n in [1, 3, 5, 10, 15, 30]:
            rows[f"atm_oi_change_{n}m"] = ce_oi.diff(n) + pe_oi.diff(n)

    # OI velocity vs futures (use total CE+PE OI)
    if ce_oi_series and pe_oi_series:
        combined_oi = total_ce_oi + total_pe_oi
        rows["fut_flow_oi_change_1m"] = combined_oi.diff(1)
        rows["fut_flow_oi_change_5m"] = combined_oi.diff(5)
        safe_mean = combined_oi.rolling(20).mean().replace(0, np.nan)
        rows["fut_flow_oi_rel_20"]     = combined_oi / safe_mean
        rows["fut_flow_oi_zscore_20"]  = (
            (combined_oi - combined_oi.rolling(20).mean())
            / combined_oi.rolling(20).std().replace(0, np.nan)
        )

    # ── IV enrichment (raw inputs for velocity layer) ────────────────────
    if "atm_ce_iv" in rows.columns and "atm_pe_iv" in rows.columns:
        rows["atm_iv"]  = (rows["atm_ce_iv"] + rows["atm_pe_iv"]) / 2
        if "iv_skew" not in rows.columns:
            rows["iv_skew"] = rows["atm_ce_iv"] - rows["atm_pe_iv"]
        rows["iv_pct_rank_session"] = rows["atm_iv"].rank(pct=True)

    if "atm_ce_ltp" in rows.columns and "atm_pe_ltp" in rows.columns:
        rows["atm_straddle_premium"]   = rows["atm_ce_ltp"] + rows["atm_pe_ltp"]
        rows["atm_ce_pe_premium_ratio"] = (
            rows["atm_ce_ltp"] / rows["atm_pe_ltp"].replace(0, np.nan)
        )

    # ── Feature engine — all derived features (L1-L6) ────────────────────
    # Same build_features() call used by the live runtime.
    # Layers: returns → technicals → session → velocity → compression → context
    rows = build_features(
        rows,
        trade_date=trade_date,
        prev_day_close=prev_day_close,
        vix_open=vix_open_val,
        expiry_date=expiry_date,   # monthly expiry (raw data has no expiry column)
    )

    return rows.reset_index()


def _verify_indicators(indicators_dir: Path, sample_dates: List[date]):
    """Spot-check a few days of indicator output for sanity."""
    print(f"\n{'='*60}")
    print("INDICATOR VERIFICATION (spot-check)")
    print(f"{'='*60}")

    for td in sample_dates:
        fpath = indicators_dir / f"{td.isoformat()}.parquet"
        if not fpath.exists():
            print(f"  {td}: MISSING")
            continue
        df = pd.read_parquet(fpath)
        if df.empty:
            print(f"  {td}: EMPTY")
            continue

        issues = []

        if len(df) not in (375, 376):
            issues.append(f"bar_count={len(df)} (expected 375/376)")

        if "px_fut_close" in df.columns:
            sc = df["px_fut_close"].dropna()
            if sc.empty:
                issues.append("px_fut_close all NaN")
            elif sc.min() < 1000 or sc.max() > 200000:
                issues.append(f"px_fut_close range [{sc.min():.0f},{sc.max():.0f}] suspicious")

        if "vix" in df.columns:
            vix = df["vix"].dropna()
            if not vix.empty and (vix.min() < 5 or vix.max() > 100):
                issues.append(f"vix range [{vix.min():.1f},{vix.max():.1f}] suspicious")

        if "opt_flow_pcr_oi" in df.columns:
            pcr = df["opt_flow_pcr_oi"].dropna()
            if not pcr.empty and (pcr.min() < 0.05 or pcr.max() > 20):
                issues.append(f"pcr range [{pcr.min():.2f},{pcr.max():.2f}] suspicious")

        if "comp_atr_compression" in df.columns:
            comp = df["comp_atr_compression"].dropna()
            if not comp.empty and comp.mean() > 2.0:
                issues.append(f"comp_atr_compression mean={comp.mean():.2f} high")

        key_cols = ["px_fut_close", "atm_iv", "atm_ce_oi", "opt_flow_pcr_oi", "vix",
                    "osc_atr_14", "ema_9", "vel_price_delta_open", "comp_atr_compression"]
        nan_report = {c: f"{df[c].isna().mean()*100:.0f}%" for c in key_cols if c in df}

        status = "WARN " if issues else "OK   "
        print(f"  {td}: {status} bars={len(df)} NaN={nan_report}")
        for issue in issues:
            print(f"         ! {issue}")
    print()


# ── Step 3: ASSEMBLE FINAL SNAPSHOT ──────────────────────────────────────────

def run_assemble(args):
    """Step 3: Combine day indicator files into final training parquet."""
    indicators_dir = Path(args.indicators_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    day_files = sorted(indicators_dir.glob("*.parquet"))
    if not day_files:
        log.error("No indicator files found in %s", indicators_dir)
        sys.exit(1)

    log.info("Assembling %d day files -> %s", len(day_files), out_dir)

    # Load and combine
    parts = []
    for f in day_files:
        try:
            df = pd.read_parquet(f)
            if not df.empty:
                parts.append(df)
        except Exception as e:
            log.warning("Failed to load %s: %s", f.name, e)

    if not parts:
        log.error("All indicator files empty or corrupt")
        sys.exit(1)

    combined = pd.concat(parts, ignore_index=True)
    log.info("Total rows: %d across %d days", len(combined), len(parts))

    # Save full dataset
    full_path = out_dir / "snapshots_dhan_v1.parquet"
    combined.to_parquet(full_path, index=False, engine="pyarrow")
    log.info("Saved full dataset -> %s (%.1f MB)", full_path, full_path.stat().st_size / 1e6)

    # Also save year-partitioned files (easier for training window selection)
    if "trade_date" in combined.columns:
        combined["year"] = pd.to_datetime(combined["trade_date"]).dt.year
        for yr, grp in combined.groupby("year"):
            yr_path = out_dir / f"snapshots_dhan_v1_{yr}.parquet"
            grp.drop(columns=["year"]).to_parquet(yr_path, index=False)
            log.info("  Year %d: %d rows -> %s", yr, len(grp), yr_path.name)

    _verify_final_snapshot(combined)


def _verify_final_snapshot(df: pd.DataFrame):
    """Final quality gate on the assembled snapshot."""
    print(f"\n{'='*60}")
    print("FINAL SNAPSHOT VERIFICATION (snapshots_dhan_v1)")
    print(f"{'='*60}")

    if "trade_date" in df.columns:
        dates = pd.to_datetime(df["trade_date"])
        print(f"  Date range: {dates.min().date()} -> {dates.max().date()}")
        print(f"  Trading days: {dates.nunique()}")
        print(f"  Total bars: {len(df):,}")
        print(f"  Avg bars/day: {len(df) / dates.nunique():.0f}")

    print("\n  Column coverage and NaN rates:")
    key_groups = {
        "Price (v2 names)":   ["px_fut_open", "px_fut_high", "px_fut_low", "px_fut_close"],
        "Returns":            ["ret_1m", "ret_5m", "ret_15m", "ret_open"],
        "Technicals":         ["ema_9", "ema_21", "osc_rsi_14", "osc_atr_14", "adx_14"],
        "VWAP/ORB":           ["vwap_fut", "vwap_distance", "ctx_opening_range_breakout_up"],
        "VIX":                ["vix", "vix_intraday_chg", "ctx_is_high_vix_day"],
        "ATM option":         ["atm_ce_ltp", "atm_pe_ltp", "atm_ce_iv", "atm_pe_iv"],
        "OI (v2 names)":      ["opt_flow_ce_oi_total", "opt_flow_pe_oi_total", "opt_flow_pcr_oi"],
        "OI flow":            ["fut_flow_oi_change_1m", "fut_flow_oi_change_5m", "pcr_change_5m"],
        "Velocity (vel_*)":   ["vel_price_delta_open", "vel_ce_oi_delta_open", "vel_pcr_delta_open"],
        "Compression (comp_*)": ["comp_atr_compression", "comp_range_5m", "comp_bars_since_expansion"],
        "Context (ctx_*)":    ["ctx_dte_days", "ctx_is_expiry_day", "ctx_regime_trend_up"],
        "Time":               ["time_minute_index", "time_minute_of_day", "time_day_of_week"],
    }
    all_missing = []
    for group, cols in key_groups.items():
        available = [c for c in cols if c in df.columns]
        missing   = [c for c in cols if c not in df.columns]
        nan_rates = {c: f"{df[c].isna().mean()*100:.0f}%" for c in available}
        status = "OK" if not missing else f"MISS {len(missing)}/{len(cols)}"
        print(f"  [{status:12s}] {group}: {nan_rates}")
        all_missing.extend(missing)

    if all_missing:
        print(f"\n  MISSING columns ({len(all_missing)}): {all_missing[:20]}")

    print(f"\n  Total columns: {len(df.columns)}")
    print(f"  Memory: {df.memory_usage(deep=True).sum() / 1e6:.1f} MB")
    print()


# ── Step: VERIFY (standalone) ─────────────────────────────────────────────────

def run_verify(args):
    stage = args.stage
    if stage == "fetch":
        raw_dir = Path(args.raw_dir)
        _print_fetch_summary(raw_dir, args.instrument, date(2020,1,1), date.today())
    elif stage == "build":
        ind_dir = Path(args.indicators_dir)
        files = sorted(ind_dir.glob("*.parquet"))
        dates = [date.fromisoformat(f.stem) for f in files if f.stem.count("-") == 2]
        _verify_indicators(ind_dir, dates[:10])
    elif stage == "assemble":
        snap_dir = Path(args.out_dir)
        path = snap_dir / "snapshots_dhan_v1.parquet"
        df = pd.read_parquet(path)
        _verify_final_snapshot(df)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dhan Historical Data Pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # fetch
    p_fetch = sub.add_parser("fetch", help="Step 1: Download raw data from Dhan")
    p_fetch.add_argument("--instrument", default="BANKNIFTY", choices=list(INSTRUMENTS))
    p_fetch.add_argument("--start",  required=True, help="YYYY-MM-DD")
    p_fetch.add_argument("--end",    required=True, help="YYYY-MM-DD")
    p_fetch.add_argument("--token",  required=True, help="Dhan access token")
    p_fetch.add_argument("--client-id", required=True, help="Dhan client ID (1111957145)")
    p_fetch.add_argument("--out-dir", default=".data/dhan_pipeline")
    p_fetch.add_argument("--strikes", default=",".join(DEFAULT_STRIKES))
    p_fetch.add_argument("--dry-run", action="store_true")
    p_fetch.set_defaults(func=run_fetch)

    # build
    p_build = sub.add_parser("build", help="Step 2: Compute indicators from raw data")
    p_build.add_argument("--raw-dir",  required=True)
    p_build.add_argument("--out-dir",  required=True)
    # Default 2024-11-01: scope to the MONTHLY BankNifty regime. The weekly series was
    # discontinued ~Nov 2024 (DTE 0-7 weekly -> DTE 0-30 monthly = different instrument).
    # Train-on-what-you-serve: live is monthly-only. See plan doc §8a. Set "" for full span.
    p_build.add_argument("--start-date", default="2024-11-01",
                         help="Only build days >= this (YYYY-MM-DD). Default scopes to monthly regime.")
    p_build.set_defaults(func=run_build)

    # assemble
    p_assm = sub.add_parser("assemble", help="Step 3: Combine into final training parquet")
    p_assm.add_argument("--indicators-dir", required=True)
    p_assm.add_argument("--out-dir",        required=True)
    p_assm.set_defaults(func=run_assemble)

    # verify
    p_verify = sub.add_parser("verify", help="Verify output of any stage")
    p_verify.add_argument("--stage", required=True, choices=["fetch", "build", "assemble"])
    p_verify.add_argument("--raw-dir",        default="")
    p_verify.add_argument("--indicators-dir", default="")
    p_verify.add_argument("--out-dir",        default="")
    p_verify.add_argument("--instrument",     default="BANKNIFTY")
    p_verify.set_defaults(func=run_verify)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    args.func(args)


if __name__ == "__main__":
    main()
