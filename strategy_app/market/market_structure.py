"""Market-structure annotator for decision traces.

Purpose
-------
Attach a compact, JSON-safe `market_structure` block to every decision trace so a
human — or an LLM reading the clean log — can immediately judge *where in the tape*
a decision was made: were we near the intraday high or low, did we catch a breakout
or fade a fakeout, is the swing structure trending or chopping, and does momentum
confirm or diverge from that read.

This is the "bottoms / highs / breakouts" lens. The 2026-06-04 live book bought CE
three times near the intraday high of a range-bound tape that the regime tagger had
mislabelled TRENDING; a structure annotation makes that error obvious without any
model introspection.

Design
------
Stateful, session-scoped, modelled on RollingFeatureState
(`strategy_app/ml/rolling_feature_state.py`): the engine feeds one fut OHLC bar per
tick via `update(snap)`, and `snapshot()` returns the current structure read. Call
`reset()` / `on_session_start()` at the start of each replay run for reproducibility.

Everything here is derived from data the engine already has on the SnapshotAccessor
(fut OHLC, multi-horizon returns, ORB, VWAP, prev-day H/L). The only genuinely new
computation is swing-pivot (fractal) detection over the rolling highs/lows.
"""
from __future__ import annotations

import os
from collections import deque
from datetime import date
from typing import Any, Optional

from .snapshot_accessor import SnapshotAccessor


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        if out != out:  # NaN
            return None
        return out
    except (TypeError, ValueError):
        return None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class MarketStructureTracker:
    """Incremental intraday market-structure reader.

    Thresholds are env-tunable (MARKET_STRUCT_*) so the labels can be calibrated
    without code changes.
    """

    def __init__(
        self,
        *,
        max_bars: int = 240,
        breakout_lookback: int = 20,
        pivot_k: int = 2,
    ) -> None:
        self._max_bars = max(8, int(max_bars))
        self._breakout_lookback = max(3, _env_int("MARKET_STRUCT_BREAKOUT_LOOKBACK", breakout_lookback))
        self._pivot_k = max(1, _env_int("MARKET_STRUCT_PIVOT_K", pivot_k))
        self._near_edge = float(os.getenv("MARKET_STRUCT_NEAR_EDGE_FRAC", "") or 0.2)
        self._current_day: Optional[str] = None
        self._highs: deque[float] = deque(maxlen=self._max_bars)
        self._lows: deque[float] = deque(maxlen=self._max_bars)
        self._closes: deque[float] = deque(maxlen=self._max_bars)
        self._day_high: Optional[float] = None
        self._day_low: Optional[float] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def reset(self) -> None:
        """Clear all state — call at the start of each replay run for reproducibility."""
        self._current_day = None
        self._highs = deque(maxlen=self._max_bars)
        self._lows = deque(maxlen=self._max_bars)
        self._closes = deque(maxlen=self._max_bars)
        self._day_high = None
        self._day_low = None

    def on_session_start(self, trade_date: date | str) -> None:
        self._roll_day(str(trade_date))

    def _roll_day(self, new_day: str) -> None:
        if self._current_day != new_day:
            self._current_day = new_day
            self._highs.clear()
            self._lows.clear()
            self._closes.clear()
            self._day_high = None
            self._day_low = None

    # ── ingest ─────────────────────────────────────────────────────────────────
    def update(self, snap: SnapshotAccessor) -> None:
        td = getattr(snap, "trade_date", None)
        if td:
            self._roll_day(str(td))
        high = _f(snap.fut_high)
        low = _f(snap.fut_low)
        close = _f(snap.fut_close)
        # Fall back to close when a bar omits high/low so the deques stay aligned.
        if high is None:
            high = close
        if low is None:
            low = close
        if close is None:
            return
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)
        self._day_high = high if self._day_high is None else max(self._day_high, high)
        self._day_low = low if self._day_low is None else min(self._day_low, low)

    # ── read ─────────────────────────────────────────────────────────────────--
    def snapshot(self, snap: Optional[SnapshotAccessor] = None) -> dict[str, Any]:
        """Return the current market-structure block (JSON-safe)."""
        close = self._closes[-1] if self._closes else (_f(snap.fut_close) if snap else None)
        return {
            "bars_seen": len(self._closes),
            "position_in_range": self._position_in_range(close, snap),
            "breakout_state": self._breakout_state(snap),
            "swing_pivots": self._swing_pivots(),
            "momentum_alignment": self._momentum_alignment(snap),
        }

    # ── 1) position in range ───────────────────────────────────────────────────
    def _position_in_range(self, close: Optional[float], snap: Optional[SnapshotAccessor]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "day_high": self._day_high,
            "day_low": self._day_low,
            "close": close,
            "range_position": None,
            "label": "unknown",
        }
        if close is not None and self._day_high is not None and self._day_low is not None:
            span = self._day_high - self._day_low
            if span > 0:
                pos = (close - self._day_low) / span
                pos = max(0.0, min(1.0, pos))
                out["range_position"] = round(pos, 4)
                if pos >= 1.0 - self._near_edge:
                    out["label"] = "near_high"
                elif pos <= self._near_edge:
                    out["label"] = "near_low"
                else:
                    out["label"] = "mid"
            else:
                out["label"] = "flat"
        if snap is not None:
            out["vs_orb"] = self._vs_orb(close, snap)
            out["vs_prev_day"] = self._vs_prev_day(close, snap)
            pvw = _f(snap.price_vs_vwap)
            out["vs_vwap"] = (None if pvw is None else ("above" if pvw > 0 else ("below" if pvw < 0 else "at")))
        return out

    @staticmethod
    def _vs_orb(close: Optional[float], snap: SnapshotAccessor) -> str:
        orh, orl = _f(snap.orh), _f(snap.orl)
        if close is None or orh is None or orl is None:
            return "unknown"
        if close > orh:
            return "above_orh"
        if close < orl:
            return "below_orl"
        return "inside_or"

    @staticmethod
    def _vs_prev_day(close: Optional[float], snap: SnapshotAccessor) -> str:
        pdh, pdl = _f(snap.prev_day_high), _f(snap.prev_day_low)
        if close is None or pdh is None or pdl is None:
            return "unknown"
        if close > pdh:
            return "above_pdh"
        if close < pdl:
            return "below_pdl"
        return "inside_pd_range"

    # ── 2) breakout state ──────────────────────────────────────────────────────
    def _breakout_state(self, snap: Optional[SnapshotAccessor]) -> dict[str, Any]:
        out: dict[str, Any] = {"orb": "unknown", "range": "insufficient", "label": "unknown"}
        if snap is not None:
            if snap.or_ready:
                if snap.orh_broken and not snap.orl_broken:
                    out["orb"] = "broke_high"
                elif snap.orl_broken and not snap.orh_broken:
                    out["orb"] = "broke_low"
                elif snap.orh_broken and snap.orl_broken:
                    out["orb"] = "both_broken"
                else:
                    out["orb"] = "inside"
            else:
                out["orb"] = "not_ready"

        n = self._breakout_lookback
        # Need the prior window plus the current bar.
        if len(self._closes) >= n + 1:
            prior_high = max(list(self._highs)[-(n + 1):-1])
            prior_low = min(list(self._lows)[-(n + 1):-1])
            cur_close = self._closes[-1]
            cur_high = self._highs[-1]
            cur_low = self._lows[-1]
            label = "inside_range"
            if cur_close > prior_high:
                label = "breakout_up"
            elif cur_close < prior_low:
                label = "breakout_down"
            elif cur_high > prior_high:
                label = "fakeout_up"   # poked above the range, closed back inside
            elif cur_low < prior_low:
                label = "fakeout_down"
            out["range"] = label
            out["label"] = label
            out["prior_high"] = prior_high
            out["prior_low"] = prior_low
        return out

    # ── 3) swing pivots ────────────────────────────────────────────────────────
    def _swing_pivots(self) -> dict[str, Any]:
        k = self._pivot_k
        highs = list(self._highs)
        lows = list(self._lows)
        out: dict[str, Any] = {
            "structure": "insufficient",
            "swing_highs": [],
            "swing_lows": [],
            "last_swing_high": None,
            "last_swing_low": None,
        }
        if len(highs) < 2 * k + 1:
            return out
        swing_highs: list[float] = []
        swing_lows: list[float] = []
        for i in range(k, len(highs) - k):
            window_h = highs[i - k:i + k + 1]
            if highs[i] == max(window_h) and highs[i] > min(window_h):
                swing_highs.append(highs[i])
            window_l = lows[i - k:i + k + 1]
            if lows[i] == min(window_l) and lows[i] < max(window_l):
                swing_lows.append(lows[i])
        out["swing_highs"] = [round(v, 2) for v in swing_highs[-3:]]
        out["swing_lows"] = [round(v, 2) for v in swing_lows[-3:]]
        out["last_swing_high"] = round(swing_highs[-1], 2) if swing_highs else None
        out["last_swing_low"] = round(swing_lows[-1], 2) if swing_lows else None

        hh = len(swing_highs) >= 2 and swing_highs[-1] > swing_highs[-2]
        lh = len(swing_highs) >= 2 and swing_highs[-1] < swing_highs[-2]
        hl = len(swing_lows) >= 2 and swing_lows[-1] > swing_lows[-2]
        ll = len(swing_lows) >= 2 and swing_lows[-1] < swing_lows[-2]
        if hh and hl:
            out["structure"] = "uptrend"      # higher highs + higher lows
        elif lh and ll:
            out["structure"] = "downtrend"    # lower highs + lower lows
        elif len(swing_highs) >= 2 or len(swing_lows) >= 2:
            out["structure"] = "range"        # mixed / choppy
        return out

    # ── 4) momentum vs structure ────────────────────────────────────────────────
    def _momentum_alignment(self, snap: Optional[SnapshotAccessor]) -> dict[str, Any]:
        out: dict[str, Any] = {"r1m": None, "r5m": None, "r15m": None, "label": "unknown"}
        if snap is None:
            return out
        r1, r5, r15 = _f(snap.fut_return_1m), _f(snap.fut_return_5m), _f(snap.fut_return_15m)
        out["r1m"], out["r5m"], out["r15m"] = r1, r5, r15
        present = [r for r in (r1, r5, r15) if r is not None and r != 0.0]
        if not present:
            out["label"] = "flat"
        elif all(r > 0 for r in present):
            out["label"] = "aligned_up"
        elif all(r < 0 for r in present):
            out["label"] = "aligned_down"
        else:
            out["label"] = "mixed"
        return out


__all__ = ["MarketStructureTracker"]
