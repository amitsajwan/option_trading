"""Fetch a full trading day from Dhan historical APIs for replay.

Uses /v2/charts/intraday for index + futures + VIX and
/v2/charts/rollingoption for per-strike options (same as training pipeline).

Returns a dict of DataFrames mirroring the raw/ directory layout used by
dhan_build_sim_snapshots.py so it can be passed directly to build_sim_snapshots().
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# ATM offsets to fetch CE+PE for (matches dhan_build_sim_snapshots defaults)
_ATM_OFFSETS: List[int] = list(range(-5, 6))   # ATM-5 ... ATM+5

# Dhan rate limit: ~4 req/s
_REQUEST_INTERVAL = 0.28


class DhanHistoricalFetcher:
    """Fetch one trading day of market data from Dhan APIs.

    Credentials come from env (DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID) so the
    same token used by the live system is reused here.
    """

    # Index security IDs (IDX_I segment)
    _IDX = {"BANKNIFTY": "25", "NIFTY": "13"}
    _VIX_SID = "21"
    _STRIKE_STEP = {"BANKNIFTY": 100, "NIFTY": 50}

    def __init__(self, progress_cb=None):
        import requests
        self._tok = os.environ.get("DHAN_ACCESS_TOKEN", "")
        self._cid = os.environ.get("DHAN_CLIENT_ID", "1111957145")
        if not self._tok:
            raise ValueError("DHAN_ACCESS_TOKEN not set — cannot fetch historical data")
        self._sess = requests.Session()
        self._sess.headers.update({
            "access-token": self._tok,
            "client-id": self._cid,
            "Content-Type": "application/json",
        })
        self._last_req = 0.0
        self._progress_cb = progress_cb  # callable(step, msg) for progress reporting

    def _progress(self, step: str, msg: str):
        if self._progress_cb:
            self._progress_cb(step, msg)
        logger.info("%s: %s", step, msg)

    def _throttle(self):
        gap = time.monotonic() - self._last_req
        if gap < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - gap)
        self._last_req = time.monotonic()

    def _post(self, path: str, payload: dict, retries: int = 3) -> dict:
        url = f"https://api.dhan.co/v2{path}"
        for attempt in range(retries):
            self._throttle()
            try:
                r = self._sess.post(url, json=payload, timeout=30)
                if r.status_code == 429:
                    wait = 2 ** attempt * 3
                    logger.warning("429 rate limit — sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                if attempt == retries - 1:
                    raise
                logger.warning("request failed attempt %d: %s", attempt + 1, exc)
                time.sleep(1.5 ** attempt)
        return {}

    def _fetch_intraday(self, security_id: str, segment: str,
                        instrument_type: str, trade_date: str) -> List[dict]:
        """Fetch 1-min OHLCV for one security on one date."""
        resp = self._post("/charts/intraday", {
            "securityId": security_id,
            "exchangeSegment": segment,
            "instrument": instrument_type,
            "fromDate": trade_date,
            "toDate": trade_date,
            "interval": "1",
        })
        ts_list = resp.get("timestamp") or []
        opens = resp.get("open") or []
        highs = resp.get("high") or []
        lows = resp.get("low") or []
        closes = resp.get("close") or []
        volumes = resp.get("volume") or []
        n = len(closes)
        bars = []
        for i in range(n):
            ts = datetime.fromtimestamp(ts_list[i], tz=timezone.utc).astimezone(_IST) if ts_list else None
            bars.append({
                "ts": ts.isoformat() if ts else None,
                "open": opens[i] if i < len(opens) else None,
                "high": highs[i] if i < len(highs) else None,
                "low": lows[i] if i < len(lows) else None,
                "close": closes[i] if i < len(closes) else None,
                "volume": volumes[i] if i < len(volumes) else None,
            })
        return bars

    def _fetch_rolling_option(self, index_sid: str, fno_seg: str,
                               strike: int, option_type: str,
                               trade_date: str) -> List[dict]:
        """Fetch 1-min OHLCV + IV + OI for one strike on one date."""
        side = "ce" if option_type == "CALL" else "pe"
        resp = self._post("/charts/rollingoption", {
            "securityId": index_sid,
            "exchangeSegment": fno_seg,
            "instrument": "OPTIDX",
            "expiryCode": 1,        # nearest expiry
            "expiryFlag": "WEEK",
            "strike": strike,
            "drvOptionType": option_type,
            "requiredData": ["open", "high", "low", "close", "iv", "oi", "spot", "volume"],
            "fromDate": trade_date,
            "toDate": trade_date,
            "interval": "1",
        })
        data = (resp.get("data") or {}).get(side) or {}
        ts_list = data.get("timestamp") or []
        closes = data.get("close") or []
        ivs = data.get("iv") or []
        ois = data.get("oi") or []
        volumes = data.get("volume") or []
        spots = data.get("spot") or []
        n = len(closes)
        bars = []
        for i in range(n):
            ts = datetime.fromtimestamp(ts_list[i], tz=timezone.utc).astimezone(_IST) if ts_list else None
            bars.append({
                "ts": ts.isoformat() if ts else None,
                f"{side}_close": closes[i] if i < len(closes) else None,
                f"{side}_iv": ivs[i] if i < len(ivs) else None,
                f"{side}_oi": ois[i] if i < len(ois) else None,
                f"{side}_volume": volumes[i] if i < len(volumes) else None,
                "spot": spots[i] if i < len(spots) else None,
            })
        return bars

    def fetch_day(self, instrument: str, trade_date: str) -> Dict[str, Any]:
        """Fetch all data for one instrument/date from Dhan.

        Returns a dict with keys matching the raw/ directory layout:
            index_bars    : list[dict]   — index OHLCV
            futures_bars  : list[dict]   — futures OHLCV
            vix_bars      : list[dict]   — VIX OHLCV
            options       : dict[str, dict[str, list[dict]]]
                            {offset_label: {"ce": [...], "pe": [...]}}

        This is the same structure dhan_build_sim_snapshots.py expects.
        """
        inst = instrument.upper()
        idx_sid = self._IDX.get(inst)
        if not idx_sid:
            raise ValueError(f"Unknown instrument {inst}")
        step = self._STRIKE_STEP.get(inst, 100)
        fno_seg = "NSE_FNO"
        idx_seg = "IDX_I"

        self._progress("fetch", f"Fetching {inst} {trade_date} from Dhan")

        # 1. Index OHLC
        self._progress("fetch", "→ index OHLC")
        index_bars = self._fetch_intraday(idx_sid, idx_seg, "INDEX", trade_date)
        logger.info("index bars: %d", len(index_bars))

        # 2. VIX OHLC
        self._progress("fetch", "→ VIX OHLC")
        vix_bars = self._fetch_intraday(self._VIX_SID, idx_seg, "INDEX", trade_date)
        logger.info("VIX bars: %d", len(vix_bars))

        # 3. Futures OHLC — derive ATM strike from index mid-close
        mid_close = None
        if index_bars:
            closes = [b["close"] for b in index_bars if b["close"]]
            if closes:
                mid_close = closes[len(closes) // 2]

        # We don't have the futures security_id without scrip master on this host,
        # so we use the index as proxy (dhan_build_sim_snapshots already handles this)
        futures_bars = index_bars  # placeholder — builder uses index if futures missing

        # 4. Options: ATM±5 CE+PE via rollingoption
        options: Dict[str, Dict[str, List[dict]]] = {}
        if mid_close:
            atm_strike = round(mid_close / step) * step
            total_calls = len(_ATM_OFFSETS) * 2
            done = 0
            for offset in _ATM_OFFSETS:
                strike = atm_strike + offset * step
                label = (f"ATM" if offset == 0
                         else f"ATMp{offset}" if offset > 0
                         else f"ATMm{abs(offset)}")
                options[label] = {}
                for ot, side in [("CALL", "ce"), ("PUT", "pe")]:
                    done += 1
                    self._progress("fetch",
                        f"→ options {label} {ot} ({done}/{total_calls})")
                    try:
                        bars = self._fetch_rolling_option(
                            idx_sid, fno_seg, strike, ot, trade_date)
                        options[label][side] = bars
                        logger.debug("  %s %s: %d bars", label, ot, len(bars))
                    except Exception as exc:
                        logger.warning("option fetch failed %s %s: %s", label, ot, exc)
                        options[label][side] = []

        self._progress("fetch", f"Fetch complete — {len(options)} option offsets")
        return {
            "instrument": inst,
            "trade_date": trade_date,
            "index_bars": index_bars,
            "futures_bars": futures_bars,
            "vix_bars": vix_bars,
            "options": options,
            "atm_strike": atm_strike if mid_close else None,
            "step": step,
        }
