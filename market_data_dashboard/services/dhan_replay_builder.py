"""Convert fetched Dhan historical data into runtime snapshot_raw_json format.

Reuses the logic from ml_pipeline_2/scripts/dhan_build_sim_snapshots.py but
operates on the in-memory dict returned by DhanHistoricalFetcher.fetch_day()
instead of reading from raw/ parquet files.

Output: list of snapshot dicts with snapshot_raw_json (same format as
snapshot_app publishes live) — ready to be published via replay_runner.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# ── helpers ────────────────────────────────────────────────────────────────────

def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _ts_ist(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST)
        return dt.astimezone(_IST)
    except Exception:
        return None


# ── main builder ───────────────────────────────────────────────────────────────

def build_snapshots_from_dhan_data(
    raw: Dict[str, Any],
    progress_cb=None,
) -> List[Dict[str, Any]]:
    """Convert raw Dhan data dict into a list of snapshot dicts.

    Each snapshot has a 'snapshot_raw_json' key so replay_runner can publish
    it directly to Redis, identical to what snapshot_app produces live.
    """
    def _progress(msg: str):
        if progress_cb:
            progress_cb("build", msg)
        logger.info("build: %s", msg)

    instrument = raw.get("instrument", "NIFTY")
    trade_date = raw.get("trade_date", "")
    step = raw.get("step", 50)
    atm_strike = raw.get("atm_strike")
    index_bars = raw.get("index_bars", [])
    vix_bars = raw.get("vix_bars", [])
    options = raw.get("options", {})

    _progress(f"Building snapshots for {instrument} {trade_date} ({len(index_bars)} index bars)")

    # Build VIX lookup: ts → vix_close
    vix_by_ts: Dict[str, float] = {}
    for b in vix_bars:
        ts = b.get("ts", "")[:16]
        if ts and b.get("close"):
            vix_by_ts[ts] = float(b["close"])

    # Build options lookup: offset_label → {ts → {ce/pe fields}}
    # First collect all per-minute timestamps
    option_by_ts: Dict[str, Dict[str, Dict[str, Optional[float]]]] = {}
    for label, sides in options.items():
        for side, bars in sides.items():
            for b in bars:
                ts = b.get("ts", "")[:16]
                if not ts:
                    continue
                if ts not in option_by_ts:
                    option_by_ts[ts] = {}
                if label not in option_by_ts[ts]:
                    option_by_ts[ts][label] = {}
                for k, v in b.items():
                    if k != "ts":
                        option_by_ts[ts][label][k] = _f(v)

    # Running state for accumulators (day_high, day_low, open etc)
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    day_open: Optional[float] = None
    bars_seen = 0
    snapshots = []

    for i, bar in enumerate(index_bars):
        ts_str = bar.get("ts") or bar.get("start_at") or ""
        ts = _ts_ist(ts_str)
        if ts is None:
            continue
        ts_key = ts_str[:16]

        close = _f(bar.get("close"))
        open_ = _f(bar.get("open"))
        high = _f(bar.get("high"))
        low = _f(bar.get("low"))
        volume = _f(bar.get("volume"))

        if close is None:
            continue

        # Update day accumulators
        if day_open is None:
            day_open = open_
        if day_high is None or (high and high > day_high):
            day_high = high
        if day_low is None or (low and low < day_low):
            day_low = low
        bars_seen += 1

        # VIX for this bar
        vix_close = vix_by_ts.get(ts_key)
        vix_prev = None
        if vix_close and i > 0:
            prev_ts = (index_bars[i-1].get("ts",""))[:16]
            vix_prev = vix_by_ts.get(prev_ts)
        vix_chg = (vix_close - vix_prev) / vix_prev if (vix_close and vix_prev and vix_prev > 0) else None

        # Options for this bar
        opt_ts = option_by_ts.get(ts_key, {})

        # ATM option data
        atm_data = opt_ts.get("ATM", {})
        atm_ce_ltp = atm_data.get("ce_close")
        atm_pe_ltp = atm_data.get("pe_close")
        atm_ce_iv = atm_data.get("ce_iv")
        atm_pe_iv = atm_data.get("pe_iv")
        atm_ce_oi = atm_data.get("ce_oi")
        atm_pe_oi = atm_data.get("pe_oi")

        # Build strikes list for chain
        strikes = []
        total_ce_oi = 0.0
        total_pe_oi = 0.0
        for label, side_data in opt_ts.items():
            if not label.startswith("ATM"):
                continue
            # offset from label: ATM=0, ATMp1=+1, ATMm1=-1
            if label == "ATM":
                off = 0
            elif label.startswith("ATMp"):
                off = int(label[4:])
            elif label.startswith("ATMm"):
                off = -int(label[4:])
            else:
                continue
            sk = (atm_strike or close) + off * step if atm_strike or close else None
            ce_oi = side_data.get("ce_oi") or 0.0
            pe_oi = side_data.get("pe_oi") or 0.0
            total_ce_oi += float(ce_oi) if ce_oi else 0.0
            total_pe_oi += float(pe_oi) if pe_oi else 0.0
            strikes.append({
                "strike": sk,
                "ce_ltp": side_data.get("ce_close"),
                "ce_oi": ce_oi,
                "ce_iv": side_data.get("ce_iv"),
                "pe_ltp": side_data.get("pe_close"),
                "pe_oi": pe_oi,
                "pe_iv": side_data.get("pe_iv"),
            })

        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else None
        atm_iv = ((atm_ce_iv or 0) + (atm_pe_iv or 0)) / 2 if (atm_ce_iv and atm_pe_iv) else None

        # Build the snapshot dict in runtime format
        snapshot = {
            "schema_name": "SnapshotMLFlat",
            "schema_version": "3.0",
            "build_source": "dhan_replay",
            "build_run_id": f"replay-{trade_date}-{instrument}",
            "snapshot_id": f"{trade_date.replace('-','')}_{ts.strftime('%H%M')}",
            "instrument": f"{instrument}FUT",
            "trade_date": trade_date,
            "timestamp": ts.isoformat(),
            "session_context": {
                "snapshot_id": f"{trade_date.replace('-','')}_{ts.strftime('%H%M')}",
                "timestamp": ts.isoformat(),
                "date": trade_date,
                "time": ts.strftime("%H:%M:%S"),
                "minutes_since_open": max(0, (ts.hour * 60 + ts.minute) - (9 * 60 + 15)),
                "minutes_to_close": max(0, (15 * 60 + 30) - (ts.hour * 60 + ts.minute)),
                "day_of_week": ts.weekday(),
                "days_to_expiry": None,
                "is_expiry_day": None,
                "session_phase": _session_phase(ts),
                "is_first_hour": ts.hour == 9,
                "is_last_hour": ts.hour >= 14,
            },
            "futures_bar": {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            },
            "futures_derived": {
                "fut_return_1m": _pct_chg(index_bars, i, 1),
                "fut_return_5m": _pct_chg(index_bars, i, 5),
                "fut_return_15m": _pct_chg(index_bars, i, 15),
                "fut_return_30m": _pct_chg(index_bars, i, 30),
                "dist_from_day_high": (close - day_high) / day_high if (close and day_high and day_high > 0) else None,
                "dist_from_day_low": (close - day_low) / day_low if (close and day_low and day_low > 0) else None,
                "atr_ratio": _atr_ratio(index_bars, i, 14),
            },
            "atm_options": {
                "atm_ce_ltp": atm_ce_ltp,
                "atm_pe_ltp": atm_pe_ltp,
                "atm_ce_iv": atm_ce_iv,
                "atm_pe_iv": atm_pe_iv,
                "atm_ce_oi": atm_ce_oi,
                "atm_pe_oi": atm_pe_oi,
                "atm_iv": atm_iv,
                "iv_skew": (atm_ce_iv - atm_pe_iv) if (atm_ce_iv and atm_pe_iv) else None,
            },
            "chain_aggregates": {
                "pcr": pcr,
                "pcr_change_5m": None,
                "atm_oi_ratio": (atm_ce_oi / atm_pe_oi) if (atm_ce_oi and atm_pe_oi and atm_pe_oi > 0) else None,
                "near_atm_oi_ratio": None,
            },
            "vix_context": {
                "vix_current": vix_close,
                "vix_intraday_chg": vix_chg,
                "vix_regime": None,
                "vix_spike_flag": False,
            },
            "strikes": strikes,
        }
        snapshots.append(snapshot)

    _progress(f"Built {len(snapshots)} snapshots")
    return snapshots


def _session_phase(ts: datetime) -> str:
    m = ts.hour * 60 + ts.minute
    if m < 9 * 60 + 45:
        return "PRE_OPEN"
    if m < 11 * 60 + 30:
        return "MORNING"
    if m < 14 * 60 + 30:
        return "ACTIVE"
    if m < 15 * 60 + 15:
        return "PRE_CLOSE"
    return "CLOSED"


def _pct_chg(bars: List[dict], i: int, n: int) -> Optional[float]:
    if i < n:
        return None
    curr = _f(bars[i].get("close"))
    prev = _f(bars[i - n].get("close"))
    if curr and prev and prev > 0:
        return (curr - prev) / prev
    return None


def _atr_ratio(bars: List[dict], i: int, period: int = 14) -> Optional[float]:
    start = max(0, i - period)
    window = bars[start:i + 1]
    if len(window) < 3:
        return None
    trs = []
    for j in range(1, len(window)):
        h = _f(window[j].get("high"))
        l = _f(window[j].get("low"))
        pc = _f(window[j-1].get("close"))
        if h and l and pc:
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    atr = sum(trs) / len(trs)
    close = _f(bars[i].get("close"))
    return atr / close if (close and close > 0) else None
