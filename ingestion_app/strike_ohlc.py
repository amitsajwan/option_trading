"""Per-strike 1-min OHLC accumulator (forward exit-fidelity fix).

Problem (probed 2026-06-07): per-strike rows in the snapshot carry only LTP — the option
high/low are NaN, so backtested exits are stuck at 1-min-close granularity. Kite gives us
last_price per strike on each quote() but only DAY ohlc, not 1-min. So we build the 1-min
bar ourselves by sampling last_price every few seconds and tracking open/high/low/close
within each IST minute, per strike, per side.

Pure + deterministic — the sampler (api_service) feeds it ticks; get_options_chain reads the
current minute's bar. No I/O here so it is fully unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def _floor_minute(epoch_s: float) -> int:
    return int(epoch_s // 60) * 60


@dataclass
class _Bar:
    open: float
    high: float
    low: float
    close: float

    def update(self, px: float) -> None:
        self.high = max(self.high, px)
        self.low = min(self.low, px)
        self.close = px

    def as_dict(self) -> dict[str, float]:
        return {"open": self.open, "high": self.high, "low": self.low, "close": self.close}


@dataclass
class StrikeOhlcAccumulator:
    """Maintains the in-progress 1-min OHLC per (strike, side). Thread-unsafe by design —
    drive it from a single sampler loop. On minute rollover the prior bar is retained as the
    last completed bar so a snapshot built just after the boundary still sees a full minute."""

    _cur: dict[tuple[int, str], _Bar] = field(default_factory=dict)        # (strike, side) -> in-progress bar
    _cur_minute: dict[tuple[int, str], int] = field(default_factory=dict)  # (strike, side) -> minute epoch
    _last: dict[tuple[int, str], _Bar] = field(default_factory=dict)       # (strike, side) -> last completed bar
    _last_minute: dict[tuple[int, str], int] = field(default_factory=dict)

    def update(self, strike: int, side: str, price: Optional[float], epoch_s: float) -> None:
        if price is None or not (price == price) or price <= 0:   # skip None/NaN/non-positive
            return
        key = (int(strike), str(side).upper())
        minute = _floor_minute(epoch_s)
        cur_min = self._cur_minute.get(key)
        if cur_min is None or minute > cur_min:
            if cur_min is not None and key in self._cur:           # roll the completed minute
                self._last[key] = self._cur[key]
                self._last_minute[key] = cur_min
            self._cur[key] = _Bar(price, price, price, price)
            self._cur_minute[key] = minute
        else:
            self._cur[key].update(price)

    def bar(self, strike: int, side: str, *, prefer_minute: Optional[int] = None) -> Optional[dict[str, float]]:
        """Return the OHLC dict for (strike, side). If ``prefer_minute`` is given, return the
        bar matching that minute (current or last); else the in-progress current bar."""
        key = (int(strike), str(side).upper())
        if prefer_minute is not None:
            if self._cur_minute.get(key) == prefer_minute and key in self._cur:
                return self._cur[key].as_dict()
            if self._last_minute.get(key) == prefer_minute and key in self._last:
                return self._last[key].as_dict()
            return None
        if key in self._cur:
            return self._cur[key].as_dict()
        return None

    def prune(self, before_epoch_s: float) -> None:
        """Drop bars whose minute is older than ``before_epoch_s`` (keep memory bounded)."""
        cutoff = _floor_minute(before_epoch_s)
        for store, minutes in ((self._cur, self._cur_minute), (self._last, self._last_minute)):
            stale = [k for k, m in minutes.items() if m < cutoff]
            for k in stale:
                store.pop(k, None)
                minutes.pop(k, None)


__all__ = ["StrikeOhlcAccumulator"]
