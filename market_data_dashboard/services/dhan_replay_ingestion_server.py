"""Mini HTTP server that impersonates ingestion_app for historical replay.

Serves the three endpoints LiveMarketSnapshotBuilder calls:
  GET /api/v1/ohlc/{symbol}?timeframe=1m&limit=N&order=asc
  GET /api/v1/market/tick/{symbol}
  GET /api/v1/options/chain/{symbol}

The caller advances the bar pointer with set_bar(i), then calls
LiveMarketSnapshotBuilder.build_snapshot() — which hits this server
and gets data as-of bar i.  All computation (velocity, compression,
VWAP, ORB, PCR rolling, IV ranks) runs inside snapshot_app exactly
as it does live.  The only difference is source="dhan_replay" in the
published event envelope.

Usage:
    raw = requests.get("http://ingestion_app:8004/api/v1/historical/day/BANKNIFTY?date=2026-06-29").json()
    srv = DhanReplayIngestionServer(raw)
    with srv:
        builder = LiveMarketSnapshotBuilder(
            instrument="BANKNIFTY",
            market_api_base=srv.base_url,
        )
        for i in range(len(raw["index_bars"])):
            srv.set_bar(i)
            snapshot = builder.build_snapshot()
            ...
"""
from __future__ import annotations

import json
import logging
import math
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

_IST_OFFSET = "+05:30"


def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DhanReplayIngestionServer:
    """Context-manager wrapper around the mini HTTP server.

    raw_data: dict returned by ingestion_app /api/v1/historical/day/{instrument}?date=...
    """

    def __init__(self, raw_data: Dict[str, Any],
                 prev_day_bars: Optional[List[Dict]] = None,
                 expiry_date: Optional[str] = None) -> None:
        self._raw = raw_data
        self._index_bars: List[Dict] = raw_data.get("index_bars") or []
        self._vix_bars:   List[Dict] = raw_data.get("vix_bars")   or []
        self._options:    Dict       = raw_data.get("options")     or {}
        self._instrument: str = (raw_data.get("instrument") or "BANKNIFTY").upper()
        self._step: int   = int(raw_data.get("step") or 100)
        self._atm: Optional[int] = raw_data.get("atm_strike")
        # 2026-07-15 fix: options dict keyed by absolute strike (int) instead of
        # ATM-relative label ("ATMp1") — the label form was ambiguous whenever
        # atm_strike drifted intraday, since the same label could represent
        # different absolute strikes at different times of day within one
        # stored series. See _build_raw's docstring in
        # ops/dhan_replay_publish_multiday_from_mongo.py for the confirmed bug.
        self._by_strike: bool = bool(raw_data.get("options_keyed_by_strike"))
        self._prev_day_bars: List[Dict] = prev_day_bars or []
        # Real expiry date (YYYY-MM-DD) so DTE is correct even with holiday-shifted expiries
        self._expiry_date: Optional[str] = expiry_date
        self._bar_idx: int = 0
        self._lock = threading.Lock()
        self._port = _free_port()
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        # Build timestamp-indexed option lookup so _option_chain_at doesn't
        # rely on positional alignment between index_bars and option_bars.
        # opt_by_ts[label][ts_prefix]["ce"|"pe"] = bar_dict
        self._opt_by_ts: Dict[str, Dict[str, Dict[str, dict]]] = {}
        n_opt_bars = 0
        for label, sides in self._options.items():
            self._opt_by_ts[label] = {}
            for side, bars in sides.items():
                if not isinstance(bars, list):
                    continue
                for b in bars:
                    ts = (b.get("ts") or b.get("start_at") or "")[:16]
                    if not ts:
                        continue
                    if ts not in self._opt_by_ts[label]:
                        self._opt_by_ts[label][ts] = {}
                    self._opt_by_ts[label][ts][side] = b
                    n_opt_bars += 1
        if n_opt_bars == 0 and self._options:
            logger.warning(
                "DhanReplayIngestionServer: options dict has %d labels but 0 bars — "
                "ce_ltp/pe_ltp will be null for all bars. Check ingestion_app logs.",
                len(self._options),
            )
        elif n_opt_bars == 0:
            logger.warning(
                "DhanReplayIngestionServer: no option data (options={}) — "
                "atm_ce_close will be null → premium=null → 0 trades expected."
            )
        else:
            logger.info(
                "DhanReplayIngestionServer: loaded %d option bars across %d labels (ts-indexed)",
                n_opt_bars, len(self._opt_by_ts),
            )

    # ── public interface ────────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def set_bar(self, idx: int) -> None:
        with self._lock:
            self._bar_idx = idx

    def set_day(self, raw_data: Dict[str, Any],
                prev_day_bars: Optional[List[Dict]] = None,
                expiry_date: Optional[str] = None) -> None:
        """Swap in a new day's data on the SAME running server/port, so a
        multi-day continuous caller can keep ONE LiveMarketSnapshotBuilder
        instance alive across day boundaries (2026-07-15: per-day-process
        replay was found to cold-start builder.state.iv_history_* every
        single day, pinning iv_percentile near 100 and blocking almost every
        bar at IV_FILTER — a replay-only artifact, not a real finding).
        Safe without extra locking: the handler reads these attributes
        synchronously in response to this same thread's build_snapshot()
        calls — never call this while a request could be mid-flight."""
        self._raw = raw_data
        self._index_bars = raw_data.get("index_bars") or []
        self._vix_bars = raw_data.get("vix_bars") or []
        self._options = raw_data.get("options") or {}
        self._instrument = (raw_data.get("instrument") or self._instrument).upper()
        self._step = int(raw_data.get("step") or self._step)
        self._atm = raw_data.get("atm_strike")
        self._by_strike = bool(raw_data.get("options_keyed_by_strike"))
        self._prev_day_bars = prev_day_bars or []
        self._expiry_date = expiry_date
        self._bar_idx = 0

        self._opt_by_ts = {}
        for label, sides in self._options.items():
            self._opt_by_ts[label] = {}
            for side, bars in sides.items():
                if not isinstance(bars, list):
                    continue
                for b in bars:
                    ts = (b.get("ts") or b.get("start_at") or "")[:16]
                    if not ts:
                        continue
                    if ts not in self._opt_by_ts[label]:
                        self._opt_by_ts[label][ts] = {}
                    self._opt_by_ts[label][ts][side] = b

    def start(self) -> None:
        handler = self._make_handler()
        self._server = HTTPServer(("127.0.0.1", self._port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("DhanReplayIngestionServer listening on %s", self.base_url)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ── internal helpers ────────────────────────────────────────────────────

    def _bars_up_to(self, idx: int) -> List[Dict]:
        """Return prev-day bars + today's bars 0..idx, normalized to start_at key."""
        result = []
        # Prepend yesterday's bars so ctx_gap_* features can be computed
        for b in self._prev_day_bars:
            ts = b.get("ts") or b.get("start_at") or ""
            result.append({
                "start_at":  ts,
                "open":      _f(b.get("open")),
                "high":      _f(b.get("high")),
                "low":       _f(b.get("low")),
                "close":     _f(b.get("close")),
                "volume":    _f(b.get("volume")),
                "instrument": self._instrument,
                "timeframe":  "1m",
            })
        for b in self._index_bars[:idx + 1]:
            ts = b.get("ts") or b.get("start_at") or ""
            result.append({
                "start_at":  ts,
                "open":      _f(b.get("open")),
                "high":      _f(b.get("high")),
                "low":       _f(b.get("low")),
                "close":     _f(b.get("close")),
                "volume":    _f(b.get("volume")),
                "instrument": self._instrument,
                "timeframe":  "1m",
            })
        return result

    def _vix_at(self, idx: int) -> Optional[float]:
        if idx < len(self._vix_bars):
            return _f(self._vix_bars[idx].get("close"))
        return None

    def _option_chain_at(self, idx: int) -> Dict[str, Any]:
        """Build an option chain dict from the options data at bar idx.

        Uses timestamp-based lookup (not positional) so index_bars and option_bars
        don't need to be exactly aligned in length or start time.
        """
        bar = self._index_bars[idx] if idx < len(self._index_bars) else {}
        spot = _f(bar.get("close") or bar.get("close")) or 0.0
        ts = bar.get("ts") or bar.get("start_at") or ""
        ts_key = ts[:16]  # YYYY-MM-DDTHH:MM — used for timestamp lookup

        strikes = []
        total_ce_oi = 0.0
        total_pe_oi = 0.0

        for key, sides_by_ts in self._opt_by_ts.items():
            if self._by_strike:
                # key IS the absolute strike — unambiguous, no atm-drift risk.
                strike_price = key
            else:
                # Legacy label form (kept for backward compat with any other
                # caller still producing ATM-relative raw_data). Ambiguous
                # whenever atm_strike drifts intraday — see _build_raw docstring.
                label = key
                if label == "ATM":
                    offset = 0
                elif label.startswith("ATMp"):
                    try:
                        offset = int(label[4:])
                    except ValueError:
                        continue
                elif label.startswith("ATMm"):
                    try:
                        offset = -int(label[4:])
                    except ValueError:
                        continue
                else:
                    continue
                strike_price = (self._atm or round(spot / self._step) * self._step) + offset * self._step

            side_data = sides_by_ts.get(ts_key, {})
            ce_bar = side_data.get("ce", {})
            pe_bar = side_data.get("pe", {})

            ce_ltp    = _f(ce_bar.get("ce_close"))
            pe_ltp    = _f(pe_bar.get("pe_close"))
            ce_iv     = _f(ce_bar.get("ce_iv"))
            pe_iv     = _f(pe_bar.get("pe_iv"))
            ce_oi     = _f(ce_bar.get("ce_oi"))     or 0.0
            pe_oi     = _f(pe_bar.get("pe_oi"))     or 0.0
            ce_volume = _f(ce_bar.get("ce_volume")) or 0.0
            pe_volume = _f(pe_bar.get("pe_volume")) or 0.0

            # Skip strikes with no option data — Dhan rollingoption often only returns
            # even-offset labels (ATM, ATMm2, ATMp2 …). Including empty strikes causes
            # _nearest_strike to pick a no-data strike (nearest by price) over the real ATM.
            if ce_ltp is None and pe_ltp is None:
                continue

            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            strikes.append({
                "strike":    strike_price,
                "ce_ltp":    ce_ltp,
                "ce_iv":     ce_iv,
                "ce_oi":     ce_oi,
                "ce_volume": ce_volume,
                "pe_ltp":    pe_ltp,
                "pe_iv":     pe_iv,
                "pe_oi":     pe_oi,
                "pe_volume": pe_volume,
            })

        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else None

        # max_pain: strike where total P&L of option sellers is maximised
        max_pain_strike = None
        max_pain_val = None
        for sk in strikes:
            s = sk["strike"]
            val = sum(
                max(0.0, float(other["strike"]) - float(s)) * float(other.get("ce_oi") or 0)
                + max(0.0, float(s) - float(other["strike"])) * float(other.get("pe_oi") or 0)
                for other in strikes
            )
            if max_pain_val is None or val < max_pain_val:
                max_pain_val = val
                max_pain_strike = s

        return {
            "spot":       spot,
            "timestamp":  ts,
            "pcr":        pcr,
            "max_pain":   max_pain_strike,
            "expiry":     self._expiry_date,  # real expiry so DTE is correct
            "strikes":    strikes,
        }

    # ── request handler factory ─────────────────────────────────────────────

    def _make_handler(self):
        server_self = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # suppress access logs

            def _send_json(self, data, status=200):
                body = json.dumps(data, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/")
                qs = parse_qs(parsed.query)

                with server_self._lock:
                    idx = server_self._bar_idx

                # GET /api/v1/ohlc/{symbol} or /api/v1/market/ohlc/{symbol}
                if "/ohlc/" in path:
                    limit_str = (qs.get("limit") or ["1800"])[0]
                    try:
                        limit = int(limit_str)
                    except ValueError:
                        limit = 1800
                    bars = server_self._bars_up_to(idx)
                    if limit > 0:
                        bars = bars[-limit:]
                    order = (qs.get("order") or ["asc"])[0]
                    if order == "desc":
                        bars = list(reversed(bars))
                    self._send_json(bars)
                    return

                # GET /api/v1/market/tick/{symbol}
                if "/market/tick/" in path:
                    sym = path.split("/market/tick/")[-1].upper()
                    if "VIX" in sym:
                        vix = server_self._vix_at(idx)
                        self._send_json({"last_price": vix, "symbol": sym})
                    else:
                        bar = server_self._index_bars[idx] if idx < len(server_self._index_bars) else {}
                        close = _f(bar.get("close") or bar.get("close"))
                        self._send_json({"last_price": close, "symbol": sym})
                    return

                # GET /api/v1/options/chain/{symbol}
                if "/options/chain/" in path or "/option-chain/" in path:
                    chain = server_self._option_chain_at(idx)
                    self._send_json(chain)
                    return

                # Health / unknown
                self._send_json({"status": "replay"})

        return _Handler
