"""
Build live-fidelity runtime SNAPSHOTS from Dhan raw data, for the SIM/replay harness.

This produces the canonical `snapshots/` dataset (rows carry `snapshot_raw_json`) that
`strategy_app.sim.run_range` / `replay_day` consume — but built the LIVE way: each bar is
fed to `build_market_snapshot` (the same builder the live snapshot_app uses), causally,
with accumulating state. So replaying these is "as if live", source = historical.

Input  : Dhan raw dir (index/futures bars, VIX, 11 rolling option offsets ATM±5 CE/PE).
Output : <out-dir>/snapshots/year=YYYY/<trade_date>.parquet  (one row per minute bar).

Chain reconstruction: the Dhan option parquets are keyed by OFFSET (ATM, ATM±k) and carry
a `spot` column. Absolute strike at a bar = round(spot/step)*step + k*step. Each offset's
ce_*/pe_* fields populate one strike row in the chain dict (the exact format the live Dhan
get_option_chain returns).

Usage:
  python -m ml_pipeline_2.scripts.dhan_build_sim_snapshots \
      --raw-dir ~/dhan_pipeline/raw --out-dir ~/dhan_pipeline_sim \
      --instrument BANKNIFTY --start 2026-06-01 --end 2026-06-30
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from snapshot_app.core.market_snapshot import (
    MarketSnapshotState,
    build_market_snapshot,
    prepare_market_snapshot_window,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dhan_sim_snapshots")

IST = "Asia/Kolkata"
# offset label -> integer k (ATM=0, ATM+1=+1, ATM-1=-1, ...)
_OFFSETS: Dict[str, int] = {"ATM": 0}
for _i in range(1, 6):
    _OFFSETS[f"ATMp{_i}"] = _i
    _OFFSETS[f"ATMm{_i}"] = -_i

# instrument -> (strike_step, underlying label)
_STRIKE_STEP = {"BANKNIFTY": 100, "NIFTY": 50}


def _load_bars(raw_dir: Path) -> pd.DataFrame:
    """Futures/index bars: prefer futures.parquet (real volume), else index.parquet."""
    for name in ("futures.parquet", "index.parquet"):
        p = raw_dir / name
        if p.exists():
            df = pd.read_parquet(p).reset_index()
            tcol = next(c for c in df.columns if c in ("ts", "timestamp") or "time" in c.lower())
            df["timestamp"] = pd.to_datetime(df[tcol], utc=True).dt.tz_convert(IST)
            for c in ("open", "high", "low", "close", "volume", "oi"):
                if c not in df.columns:
                    df[c] = 0.0
            log.info("bars source: %s (%d rows)", name, len(df))
            return df[["timestamp", "open", "high", "low", "close", "volume", "oi"]].sort_values("timestamp")
    raise FileNotFoundError(f"no futures.parquet/index.parquet in {raw_dir}")


def _load_options(raw_dir: Path) -> Dict[str, Dict[str, pd.DataFrame]]:
    """offset -> {'ce': df, 'pe': df}, each indexed by IST timestamp."""
    out: Dict[str, Dict[str, pd.DataFrame]] = {}
    for label in _OFFSETS:
        sides: Dict[str, pd.DataFrame] = {}
        for side in ("ce", "pe"):
            p = raw_dir / f"option_{label}_{side}.parquet"
            if not p.exists():
                continue
            df = pd.read_parquet(p).reset_index()
            tcol = next(c for c in df.columns if c in ("ts", "timestamp") or "time" in c.lower())
            df["ts"] = pd.to_datetime(df[tcol], utc=True).dt.tz_convert(IST)
            sides[side] = df.set_index("ts")
        if sides:
            out[label] = sides
    log.info("loaded %d option offsets", len(out))
    return out


def _chain_at(ts: pd.Timestamp, options: Dict[str, Dict[str, pd.DataFrame]],
              step: int, underlying: str, expiry: str) -> Dict[str, Any]:
    """Assemble the live-format chain dict at one timestamp from the rolling offsets."""
    # spot from the ATM ce row (each option parquet carries 'spot')
    atm = options.get("ATM", {})
    spot = np.nan
    for side in ("ce", "pe"):
        if side in atm and ts in atm[side].index and "spot" in atm[side].columns:
            spot = float(atm[side].loc[ts, "spot"]); break
    if not np.isfinite(spot):
        return {"underlying": underlying, "spot": None, "expiry": expiry, "strikes": []}

    atm_strike = int(round(spot / step) * step)
    strikes: List[Dict[str, Any]] = []
    for label, k in _OFFSETS.items():
        sides = options.get(label)
        if not sides:
            continue
        strike = atm_strike + k * step
        row: Dict[str, Any] = {"strike": strike}
        for side in ("ce", "pe"):
            df = sides.get(side)
            if df is not None and ts in df.index:
                r = df.loc[ts]
                row[f"{side}_ltp"]    = float(r.get(f"{side}_close", np.nan))
                row[f"{side}_oi"]     = int(r.get(f"{side}_oi", 0) or 0)
                row[f"{side}_volume"] = int(r.get(f"{side}_volume", 0) or 0)
                row[f"{side}_iv"]     = float(r.get(f"{side}_iv", np.nan))
                row[f"{side}_bid"]    = np.nan
                row[f"{side}_ask"]    = np.nan
        strikes.append(row)

    total_ce = sum(s.get("ce_oi", 0) for s in strikes)
    total_pe = sum(s.get("pe_oi", 0) for s in strikes)
    return {
        "underlying": underlying, "spot": spot, "expiry": expiry,
        "timestamp": ts.isoformat(), "strikes": strikes,
        "pcr": (total_pe / total_ce) if total_ce > 0 else None,
        "total_ce_oi": int(total_ce), "total_pe_oi": int(total_pe),
    }


def build_sim_snapshots(raw_dir: Path, out_dir: Path, instrument: str,
                        start: date, end: date, expiry_hint: str = "") -> int:
    step = _STRIKE_STEP.get(instrument, 100)
    bars = _load_bars(raw_dir)
    options = _load_options(raw_dir)
    vix_df = None
    vp = raw_dir / "vix.parquet"
    if vp.exists():
        vix_df = pd.read_parquet(vp)

    bars["trade_date"] = bars["timestamp"].dt.date
    all_days = sorted({d for d in bars["trade_date"].unique() if start <= d <= end})
    log.info("building SIM snapshots for %d days (%s..%s)", len(all_days), start, end)

    snap_root = out_dir / "snapshots"
    written = 0
    state = MarketSnapshotState()  # carries IV/session continuity across days
    for d in all_days:
        # Bounded lookback window (not all history): current day + ~30 prior calendar
        # days gives ~20 trading days of context — enough for prev-day levels and the
        # minute-of-day vol baselines. Passing all history made each day O(140k bars).
        lo = d - pd.Timedelta(days=30).to_pytimedelta()
        window = bars[(bars["trade_date"] <= d) & (bars["trade_date"] >= lo)].copy()
        # keep trade_date as a string column (build_market_snapshot/helpers reference it)
        window["trade_date"] = window["trade_date"].astype(str)
        fut_window = window.reset_index(drop=True)
        try:
            prepared = prepare_market_snapshot_window(fut_window, current_trade_date=pd.Timestamp(d))
        except Exception as exc:
            log.warning("  %s: prepare failed: %s", d, exc); continue

        today_idx = fut_window.index[fut_window["timestamp"].dt.date == d].tolist()
        rows = []
        first_err: Optional[str] = None
        for full_idx in today_idx:
            ts = fut_window.iloc[full_idx]["timestamp"]
            chain = _chain_at(ts, options, step, instrument, expiry_hint)
            try:
                snap = build_market_snapshot(
                    instrument=instrument, ohlc=fut_window, chain=chain, state=state,
                    vix_daily=vix_df, vix_live_current=None,
                    prepared_window=prepared, current_index=full_idx,
                )
            except Exception as exc:
                if first_err is None:
                    first_err = f"{type(exc).__name__}: {exc}"
                continue
            rows.append({
                "trade_date": str(d), "timestamp": ts.isoformat(),
                "snapshot_id": ts.isoformat(),
                "snapshot_raw_json": json.dumps(snap, default=str),
            })
        if rows:
            ydir = snap_root / f"year={d.year}"; ydir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_parquet(ydir / f"{d}.parquet", index=False)
            written += len(rows)
            log.info("  %s: %d snapshots", d, len(rows))
        else:
            log.warning("  %s: 0 snapshots (first build error: %s)", d, first_err)
    log.info("SIM SNAPSHOTS COMPLETE — %d rows in %s", written, snap_root)
    return written


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--instrument", default="BANKNIFTY", choices=list(_STRIKE_STEP))
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--expiry-hint", default="", help="Optional expiry YYYY-MM-DD for the chain")
    a = ap.parse_args(argv)
    n = build_sim_snapshots(Path(a.raw_dir).expanduser(), Path(a.out_dir).expanduser(),
                            a.instrument, date.fromisoformat(a.start), date.fromisoformat(a.end),
                            a.expiry_hint)
    return 0 if n > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
