"""30-minute velocity accumulator.

Accumulates per-bar data from 09:15 IST (market open) and computes velocity
features at any point during the session — NOT anchored to 11:30. Features are
available from 09:45 onwards (after 30 bars of 1-min data).

Designed to replace / complement the 11:30-anchored LiveVelocityAccumulator for
entry models that need to fire from 09:35 onwards.

Feature groups:
  vel30_*   — delta/rate features (change from open to current bar)
  ctx30_*   — session context computed from open → now

All features are level-invariant (pct or ratio form). NaN returned when fewer
than MIN_BARS bars have been seen.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from datetime import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

_log = logging.getLogger(__name__)

# Minimum bars before features are meaningful (30 min of 1-min bars)
MIN_BARS: int = 30
# Session open time
_SESSION_OPEN: Tuple[int, int] = (9, 15)


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _delta_pct(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None or start == 0:
        return None
    d = (end - start) / abs(start)
    return d if math.isfinite(d) else None


def _pct_rank_in_list(history: List[float], value: float) -> Optional[float]:
    if not history:
        return None
    pool = history + [value]
    le = sum(1 for x in pool if x <= value)
    return 100.0 * le / len(pool)


class Velocity30mAccumulator:
    """Per-session accumulator: call .observe(bar_dict) each tick; read .features after MIN_BARS.

    bar_dict keys (all optional, None-safe):
        timestamp           ISO or pd.Timestamp
        fut_close           futures close price
        fut_high            futures bar high
        fut_low             futures bar low
        total_ce_oi         total CE open interest
        total_pe_oi         total PE open interest
        total_ce_volume     total CE option volume
        total_pe_volume     total PE option volume
        pcr                 put/call OI ratio
        atm_ce_iv           ATM CE implied vol
        atm_pe_iv           ATM PE implied vol
        vwap                futures VWAP
    """

    def __init__(self) -> None:
        self._bars: deque[Dict[str, Any]] = deque(maxlen=400)
        self._open_bar: Optional[Dict[str, Any]] = None  # first bar of session

    def reset(self) -> None:
        self._bars.clear()
        self._open_bar = None

    def observe(self, bar: Dict[str, Any]) -> None:
        ts = bar.get("timestamp")
        if ts is not None:
            try:
                t = pd.Timestamp(ts)
                if (t.hour, t.minute) < _SESSION_OPEN:
                    return  # ignore pre-market
            except Exception:
                pass
        self._bars.append(bar)
        if self._open_bar is None:
            self._open_bar = bar

    @property
    def bar_count(self) -> int:
        return len(self._bars)

    @property
    def ready(self) -> bool:
        return len(self._bars) >= MIN_BARS

    def features(self) -> Dict[str, Optional[float]]:
        """Compute velocity features from open → current bar. Returns all NaN dict if not ready."""
        nan: Dict[str, Optional[float]] = {k: None for k in VELOCITY_30M_COLUMNS}
        if not self.ready or self._open_bar is None:
            return nan

        bars = list(self._bars)
        ob = self._open_bar  # open bar
        cb = bars[-1]        # current bar
        n = len(bars)
        minutes_elapsed = max(1, n)

        out: Dict[str, Optional[float]] = {}

        # ── Price velocity ─────────────────────────────────────────────────────
        px_open = _safe_float(ob.get("fut_close"))
        px_now  = _safe_float(cb.get("fut_close"))
        out["vel30_price_chg_pct"]   = _delta_pct(px_open, px_now)

        px_30m = _safe_float(bars[max(0, n - 30)].get("fut_close")) if n >= 30 else None
        out["vel30_price_chg_30m"]   = _delta_pct(px_30m, px_now)

        # Rate: pct per minute from open
        _pdelta = _delta_pct(px_open, px_now)
        out["vel30_price_rate"]      = (_pdelta / minutes_elapsed) if _pdelta is not None else None

        # ── Session high/low range ─────────────────────────────────────────────
        highs = [_safe_float(b.get("fut_high")) for b in bars]
        lows  = [_safe_float(b.get("fut_low"))  for b in bars]
        highs_v = [h for h in highs if h is not None]
        lows_v  = [l for l in lows  if l is not None]
        sess_hi = max(highs_v) if highs_v else None
        sess_lo = min(lows_v)  if lows_v  else None
        if sess_hi and sess_lo and sess_hi > sess_lo and px_now:
            out["vel30_position_in_range"] = (px_now - sess_lo) / (sess_hi - sess_lo)
        else:
            out["vel30_position_in_range"] = None
        out["vel30_range_size_pct"] = _delta_pct(sess_lo, sess_hi) if sess_lo and sess_hi else None

        # Rising lows / falling highs structure
        if len(highs_v) >= 3 and len(lows_v) >= 3:
            rising_lows   = sum(1 for i in range(1, len(lows_v))  if lows_v[i]  > lows_v[i-1])  / max(1, len(lows_v) - 1)
            falling_highs = sum(1 for i in range(1, len(highs_v)) if highs_v[i] < highs_v[i-1]) / max(1, len(highs_v) - 1)
            out["vel30_structure_score"] = rising_lows - falling_highs  # +1 = strong bull structure
        else:
            out["vel30_structure_score"] = None

        # ── OI velocity ────────────────────────────────────────────────────────
        ce_oi_open = _safe_float(ob.get("total_ce_oi"))
        pe_oi_open = _safe_float(ob.get("total_pe_oi"))
        ce_oi_now  = _safe_float(cb.get("total_ce_oi"))
        pe_oi_now  = _safe_float(cb.get("total_pe_oi"))

        out["vel30_ce_oi_delta"] = _delta_pct(ce_oi_open, ce_oi_now)
        out["vel30_pe_oi_delta"] = _delta_pct(pe_oi_open, pe_oi_now)

        _ce_d = out["vel30_ce_oi_delta"]
        _pe_d = out["vel30_pe_oi_delta"]
        if _ce_d is not None and _pe_d is not None:
            out["vel30_oi_skew"] = _ce_d - _pe_d  # positive = CE OI growing faster
        else:
            out["vel30_oi_skew"] = None

        # Build rate (pct per minute)
        out["vel30_ce_oi_rate"] = (_ce_d / minutes_elapsed) if _ce_d is not None else None
        out["vel30_pe_oi_rate"] = (_pe_d / minutes_elapsed) if _pe_d is not None else None

        # ── PCR velocity ────────────────────────────────────────────────────────
        pcr_open = _safe_float(ob.get("pcr"))
        pcr_now  = _safe_float(cb.get("pcr"))
        out["vel30_pcr_delta"] = _delta_pct(pcr_open, pcr_now)

        pcrs = [_safe_float(b.get("pcr")) for b in bars]
        pcrs_v = [p for p in pcrs if p is not None]
        if len(pcrs_v) >= 5:
            # Trend: slope of PCR over session (+ = rising = more put = bearish)
            xs = list(range(len(pcrs_v)))
            mean_x = sum(xs) / len(xs)
            mean_y = sum(pcrs_v) / len(pcrs_v)
            num = sum((xs[i] - mean_x) * (pcrs_v[i] - mean_y) for i in range(len(xs)))
            den = sum((x - mean_x) ** 2 for x in xs)
            slope = (num / den) if den > 0 else 0.0
            # Normalise by mean_y to get dimensionless rate
            out["vel30_pcr_trend"] = (slope / mean_y) if mean_y and mean_y != 0 else None
        else:
            out["vel30_pcr_trend"] = None

        # ── IV velocity ─────────────────────────────────────────────────────────
        ce_iv_open = _safe_float(ob.get("atm_ce_iv"))
        pe_iv_open = _safe_float(ob.get("atm_pe_iv"))
        ce_iv_now  = _safe_float(cb.get("atm_ce_iv"))
        pe_iv_now  = _safe_float(cb.get("atm_pe_iv"))
        out["vel30_ce_iv_delta"] = _delta_pct(ce_iv_open, ce_iv_now)
        out["vel30_pe_iv_delta"] = _delta_pct(pe_iv_open, pe_iv_now)

        # IV compression: both CE and PE IV falling → market coiling
        _ce_iv_d = out["vel30_ce_iv_delta"]
        _pe_iv_d = out["vel30_pe_iv_delta"]
        if _ce_iv_d is not None and _pe_iv_d is not None:
            out["vel30_iv_compression"] = -(_ce_iv_d + _pe_iv_d) / 2.0  # positive = compression
        else:
            out["vel30_iv_compression"] = None

        # ── Volume velocity ──────────────────────────────────────────────────────
        ce_vol_bars = [_safe_float(b.get("total_ce_volume")) for b in bars]
        pe_vol_bars = [_safe_float(b.get("total_pe_volume")) for b in bars]
        ce_vol_now  = _safe_float(cb.get("total_ce_volume"))
        pe_vol_now  = _safe_float(cb.get("total_pe_volume"))
        ce_vol_open = _safe_float(ob.get("total_ce_volume"))
        pe_vol_open = _safe_float(ob.get("total_pe_volume"))
        out["vel30_ce_vol_delta"] = _delta_pct(ce_vol_open, ce_vol_now)
        out["vel30_pe_vol_delta"] = _delta_pct(pe_vol_open, pe_vol_now)

        # Volume acceleration: last 5 bars vs prior 5 bars
        def _vol_accel(vols: List[Optional[float]]) -> Optional[float]:
            v = [x for x in vols if x is not None]
            if len(v) < 10:
                return None
            recent = sum(v[-5:]) / 5
            prior  = sum(v[-10:-5]) / 5
            return _delta_pct(prior, recent)
        out["vel30_ce_vol_accel"] = _vol_accel(ce_vol_bars)
        out["vel30_pe_vol_accel"] = _vol_accel(pe_vol_bars)

        # ── VWAP context ─────────────────────────────────────────────────────────
        vwap_now = _safe_float(cb.get("vwap"))
        if vwap_now and px_now:
            out["vel30_price_vs_vwap"] = (px_now - vwap_now) / vwap_now
        else:
            out["vel30_price_vs_vwap"] = None

        # Consecutive bars above/below VWAP (VWAP hold strength)
        vwap_sides = []
        for b in reversed(bars):
            p = _safe_float(b.get("fut_close"))
            w = _safe_float(b.get("vwap"))
            if p and w:
                vwap_sides.append(1 if p > w else -1)
            else:
                break
        if vwap_sides:
            # All same sign = strong hold; mixed = weak
            _s = vwap_sides[0]
            streak = sum(1 for v in vwap_sides if v == _s)
            out["vel30_vwap_streak"] = float(streak * _s)  # positive = bull hold, negative = bear
        else:
            out["vel30_vwap_streak"] = None

        return {k: out.get(k) for k in VELOCITY_30M_COLUMNS}


# Canonical column list — order matters for feature arrays
VELOCITY_30M_COLUMNS = (
    "vel30_price_chg_pct",
    "vel30_price_chg_30m",
    "vel30_price_rate",
    "vel30_position_in_range",
    "vel30_range_size_pct",
    "vel30_structure_score",
    "vel30_ce_oi_delta",
    "vel30_pe_oi_delta",
    "vel30_oi_skew",
    "vel30_ce_oi_rate",
    "vel30_pe_oi_rate",
    "vel30_pcr_delta",
    "vel30_pcr_trend",
    "vel30_ce_iv_delta",
    "vel30_pe_iv_delta",
    "vel30_iv_compression",
    "vel30_ce_vol_delta",
    "vel30_pe_vol_delta",
    "vel30_ce_vol_accel",
    "vel30_pe_vol_accel",
    "vel30_price_vs_vwap",
    "vel30_vwap_streak",
)
