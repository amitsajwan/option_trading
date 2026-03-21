"""Rolling intraday feature state for pure-ML runtime feature completion."""

from __future__ import annotations

from collections import deque
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
from contracts_app.regime_thresholds import (
    ATR_PERCENTILE_HIGH,
    ATR_PERCENTILE_LOW,
    VIX_HIGH_THRESHOLD,
    VIX_LOW_THRESHOLD,
)

from .snapshot_accessor import SnapshotAccessor


def _to_float(value: object) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(out):
        return None
    return float(out)


def _pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    cur = _to_float(current)
    prev = _to_float(previous)
    if cur is None or prev is None or prev == 0.0:
        return None
    return float((cur - prev) / prev)


def _oi_ratio(call_oi: Optional[float], put_oi: Optional[float]) -> Optional[float]:
    ce = _to_float(call_oi)
    pe = _to_float(put_oi)
    if ce is None or pe is None:
        return None
    total = ce + pe
    if total <= 0.0:
        return None
    return float(ce / total)


def _near_atm_oi_ratio(snap: SnapshotAccessor) -> Optional[float]:
    raw = snap.raw_payload if isinstance(snap.raw_payload, dict) else {}
    strikes = raw.get("strikes") if isinstance(raw.get("strikes"), list) else []
    atm_strike = snap.atm_strike
    aggregate_ratio = snap.near_atm_oi_ratio
    if not strikes or atm_strike is None:
        return aggregate_ratio if aggregate_ratio is not None else _oi_ratio(snap.atm_ce_oi, snap.atm_pe_oi)
    ordered = [row for row in strikes if isinstance(row, dict) and _to_float(row.get("strike")) is not None]
    if not ordered:
        return aggregate_ratio if aggregate_ratio is not None else _oi_ratio(snap.atm_ce_oi, snap.atm_pe_oi)
    ordered = sorted(ordered, key=lambda row: float(row["strike"]))
    atm_index = min(range(len(ordered)), key=lambda idx: abs(float(ordered[idx]["strike"]) - float(atm_strike)))
    window = ordered[max(0, atm_index - 1) : min(len(ordered), atm_index + 2)]
    ce_sum = sum(_to_float(row.get("ce_oi")) or 0.0 for row in window)
    pe_sum = sum(_to_float(row.get("pe_oi")) or 0.0 for row in window)
    if ce_sum <= 0.0 and pe_sum <= 0.0:
        return aggregate_ratio if aggregate_ratio is not None else _oi_ratio(snap.atm_ce_oi, snap.atm_pe_oi)
    return _oi_ratio(ce_sum, pe_sum)


def _rsi_wilder(values: list[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    series = pd.Series(np.asarray(values, dtype=float))
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0)).abs()
    avg_gain = gain.ewm(alpha=1.0 / float(period), min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / float(period), min_periods=period, adjust=False).mean()
    g = _to_float(avg_gain.iloc[-1])
    l = _to_float(avg_loss.iloc[-1])
    if g is None or l is None:
        return None
    if l == 0.0:
        return 100.0
    rs = g / l
    return float(100.0 - (100.0 / (1.0 + rs)))


def _atr_wilder(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    prev_close = c[:-1]
    tr = np.maximum.reduce(
        [
            np.abs(h[1:] - l[1:]),
            np.abs(h[1:] - prev_close),
            np.abs(l[1:] - prev_close),
        ]
    )
    alpha = 1.0 / float(period)
    atr: Optional[float] = None
    for idx, value in enumerate(tr):
        if idx + 1 < period:
            continue
        if idx + 1 == period:
            atr = float(np.mean(tr[:period]))
            continue
        assert atr is not None
        atr = float(alpha * value + (1.0 - alpha) * atr)
    return atr


def _ema_step(prev_ema: Optional[float], value: Optional[float], span: int) -> Optional[float]:
    cur = _to_float(value)
    if cur is None:
        return prev_ema
    if prev_ema is None:
        return cur
    k = 2.0 / (float(span) + 1.0)
    return float(cur * k + prev_ema * (1.0 - k))


def _day_percentile(history: list[float], value: Optional[float]) -> Optional[float]:
    cur = _to_float(value)
    if cur is None:
        return None
    arr = [x for x in history if _to_float(x) is not None]
    if len(arr) < 5:
        return None
    a = np.asarray(arr, dtype=float)
    return float((a <= cur).sum() / len(a))


class RollingFeatureState:
    """Incremental intraday feature calculator from raw snapshot bars."""

    def __init__(
        self,
        *,
        max_bars: int = 240,
        rel_volume_window: int = 20,
        daily_atr_history_days: int = 120,
    ) -> None:
        self._max_bars = max(32, int(max_bars))
        self._rel_volume_window = max(2, int(rel_volume_window))
        self._daily_atr_history: deque[float] = deque(maxlen=max(20, int(daily_atr_history_days)))
        self._current_day: Optional[str] = None
        self._closes: deque[float] = deque(maxlen=self._max_bars)
        self._highs: deque[float] = deque(maxlen=self._max_bars)
        self._lows: deque[float] = deque(maxlen=self._max_bars)
        self._volumes: deque[float] = deque(maxlen=self._max_bars)
        self._fut_ois: deque[float] = deque(maxlen=self._max_bars)
        self._pcr_values: deque[float] = deque(maxlen=self._max_bars)
        self._atm_ce_closes: deque[float] = deque(maxlen=self._max_bars)
        self._atm_pe_closes: deque[float] = deque(maxlen=self._max_bars)
        self._option_total_volume: deque[float] = deque(maxlen=self._max_bars)
        self._day_high: Optional[float] = None
        self._day_low: Optional[float] = None
        self._vwap_num: float = 0.0
        self._vwap_den: float = 0.0
        self._ema_9: Optional[float] = None
        self._ema_21: Optional[float] = None
        self._ema_50: Optional[float] = None
        self._last_day_atr: Optional[float] = None
        self._last_atm_oi_sum: Optional[float] = None

    def on_session_start(self, trade_date: date) -> None:
        self._roll_day(str(trade_date))

    def on_session_end(self) -> None:
        if _to_float(self._last_day_atr) is not None:
            self._daily_atr_history.append(float(self._last_day_atr))
        self._current_day = None

    def _roll_day(self, new_day: str) -> None:
        if self._current_day is not None and self._current_day != new_day and _to_float(self._last_day_atr) is not None:
            self._daily_atr_history.append(float(self._last_day_atr))
        self._current_day = new_day
        self._closes.clear()
        self._highs.clear()
        self._lows.clear()
        self._volumes.clear()
        self._fut_ois.clear()
        self._pcr_values.clear()
        self._atm_ce_closes.clear()
        self._atm_pe_closes.clear()
        self._option_total_volume.clear()
        self._day_high = None
        self._day_low = None
        self._vwap_num = 0.0
        self._vwap_den = 0.0
        self._ema_9 = None
        self._ema_21 = None
        self._ema_50 = None
        self._last_day_atr = None
        self._last_atm_oi_sum = None

    def update(self, snap: SnapshotAccessor) -> dict[str, object]:
        ts = snap.timestamp
        day = str(snap.trade_date or (ts.date().isoformat() if ts is not None else "")).strip()
        if day and day != self._current_day:
            self._roll_day(day)

        close = _to_float(snap.fut_close)
        high = _to_float(snap.fut_high) or close
        low = _to_float(snap.fut_low) or close
        volume = _to_float(snap.fut_volume)
        fut_oi = _to_float(snap.fut_oi)
        if close is None:
            return {}

        prev_ema_9 = self._ema_9
        prev_ema_21 = self._ema_21
        prev_ema_50 = self._ema_50
        self._ema_9 = _ema_step(self._ema_9, close, span=9)
        self._ema_21 = _ema_step(self._ema_21, close, span=21)
        self._ema_50 = _ema_step(self._ema_50, close, span=50)

        self._closes.append(float(close))
        self._highs.append(float(high if high is not None else close))
        self._lows.append(float(low if low is not None else close))
        if volume is not None:
            self._volumes.append(float(volume))
        if fut_oi is not None:
            self._fut_ois.append(float(fut_oi))
        pcr = _to_float(snap.pcr)
        self._pcr_values.append(float(pcr) if pcr is not None else float("nan"))

        ce_close = _to_float(snap.atm_ce_close)
        pe_close = _to_float(snap.atm_pe_close)
        if ce_close is not None:
            self._atm_ce_closes.append(float(ce_close))
        if pe_close is not None:
            self._atm_pe_closes.append(float(pe_close))

        atm_total_vol = None
        if _to_float(snap.atm_ce_volume) is not None or _to_float(snap.atm_pe_volume) is not None:
            atm_total_vol = float((_to_float(snap.atm_ce_volume) or 0.0) + (_to_float(snap.atm_pe_volume) or 0.0))
            self._option_total_volume.append(atm_total_vol)

        self._day_high = float(max(self._day_high, high)) if (self._day_high is not None and high is not None) else (
            float(high) if high is not None else self._day_high
        )
        self._day_low = float(min(self._day_low, low)) if (self._day_low is not None and low is not None) else (
            float(low) if low is not None else self._day_low
        )

        if volume is not None and high is not None and low is not None:
            typical = (float(high) + float(low) + float(close)) / 3.0
            self._vwap_num += typical * float(volume)
            self._vwap_den += float(volume)
        vwap = (self._vwap_num / self._vwap_den) if self._vwap_den > 0.0 else None

        closes = list(self._closes)
        highs = list(self._highs)
        lows = list(self._lows)
        volumes = list(self._volumes)
        atr_14 = _atr_wilder(highs, lows, closes, period=14)
        if atr_14 is not None:
            self._last_day_atr = float(atr_14)
        atr_ratio = (float(atr_14) / float(close)) if (atr_14 is not None and close != 0.0) else None
        atr_daily_percentile = _day_percentile(list(self._daily_atr_history), self._last_day_atr)

        ret_1m = _pct_change(closes[-1], closes[-2] if len(closes) >= 2 else None)
        ret_3m = _pct_change(closes[-1], closes[-4] if len(closes) >= 4 else None)
        ret_5m = _pct_change(closes[-1], closes[-6] if len(closes) >= 6 else None)
        rsi_14 = _rsi_wilder(closes, period=14)
        fut_rel_volume_20 = None
        if len(volumes) >= 5:
            recent = np.asarray(volumes[-self._rel_volume_window :], dtype=float)
            denom = float(np.mean(recent)) if len(recent) > 0 else 0.0
            if denom > 0.0 and volume is not None:
                fut_rel_volume_20 = float(volume / denom)
        fut_volume_accel_1m = _pct_change(volumes[-1] if len(volumes) >= 1 else None, volumes[-2] if len(volumes) >= 2 else None)
        options_rel_volume_20 = None
        if len(self._option_total_volume) >= 5 and atm_total_vol is not None:
            opt_recent = np.asarray(list(self._option_total_volume)[-self._rel_volume_window :], dtype=float)
            opt_denom = float(np.mean(opt_recent)) if len(opt_recent) > 0 else 0.0
            if opt_denom > 0.0:
                options_rel_volume_20 = float(atm_total_vol / opt_denom)
        fut_oi_change_1m = None
        fut_oi_change_5m = None
        fut_oi_rel_20 = None
        fut_oi_zscore_20 = None
        fut_ois = list(self._fut_ois)
        if len(fut_ois) >= 2:
            fut_oi_change_1m = float(fut_ois[-1] - fut_ois[-2])
        if len(fut_ois) >= 6:
            fut_oi_change_5m = float(fut_ois[-1] - fut_ois[-6])
        if len(fut_ois) >= 5:
            oi_recent = np.asarray(fut_ois[-self._rel_volume_window :], dtype=float)
            oi_mean = float(np.mean(oi_recent)) if len(oi_recent) > 0 else 0.0
            if oi_mean != 0.0 and fut_oi is not None:
                fut_oi_rel_20 = float(fut_oi / oi_mean)
            oi_std = float(np.std(oi_recent, ddof=0)) if len(oi_recent) > 0 else 0.0
            if oi_std > 0.0 and fut_oi is not None:
                fut_oi_zscore_20 = float((fut_oi - oi_mean) / oi_std)
        pcr_change_5m = None
        pcr_change_15m = None
        if len(self._pcr_values) >= 6:
            prev_pcr_5 = _to_float(self._pcr_values[-6])
            if pcr is not None and prev_pcr_5 is not None:
                pcr_change_5m = float(pcr - prev_pcr_5)
        if len(self._pcr_values) >= 16:
            prev_pcr_15 = _to_float(self._pcr_values[-16])
            if pcr is not None and prev_pcr_15 is not None:
                pcr_change_15m = float(pcr - prev_pcr_15)

        minute_of_day = None
        day_of_week = None
        if ts is not None:
            minute_of_day = int(ts.hour * 60 + ts.minute)
            day_of_week = int(ts.weekday())
        elif snap.minutes_since_open is not None:
            minute_of_day = int(555 + int(snap.minutes_since_open))
            day_of_week = snap.day_of_week

        vix_current = _to_float(snap.vix_current)
        vix_reference = _to_float(snap.vix_prev_close)
        if vix_reference is None:
            vix_reference = vix_current
        high_vix_day = 1.0 if (vix_current is not None and vix_current >= VIX_HIGH_THRESHOLD) else 0.0
        regime_vol_high = 1.0 if (high_vix_day == 1.0 or (vix_reference is not None and vix_reference >= VIX_HIGH_THRESHOLD)) else 0.0
        regime_vol_low = 1.0 if (vix_reference is not None and vix_reference < VIX_LOW_THRESHOLD) else 0.0
        regime_vol_neutral = 1.0 if (vix_reference is not None and regime_vol_high == 0.0 and regime_vol_low == 0.0) else 0.0
        trend_up = 1.0 if (snap.fut_return_5m is not None and snap.fut_return_15m is not None and snap.fut_return_5m > 0 and snap.fut_return_15m > 0) else 0.0
        trend_down = 1.0 if (snap.fut_return_5m is not None and snap.fut_return_15m is not None and snap.fut_return_5m < 0 and snap.fut_return_15m < 0) else 0.0
        is_near_expiry = 1.0 if (snap.days_to_expiry is not None and snap.days_to_expiry <= 1) else 0.0
        prev_atm_oi_sum = self._prev_atm_oi_sum(snap) if (snap.atm_ce_oi is not None or snap.atm_pe_oi is not None) else None
        atm_oi_change_1m = None
        if prev_atm_oi_sum is not None and (snap.atm_ce_oi is not None or snap.atm_pe_oi is not None):
            current_atm_oi_sum = float((snap.atm_ce_oi or 0.0) + (snap.atm_pe_oi or 0.0))
            atm_oi_change_1m = float(current_atm_oi_sum - prev_atm_oi_sum)
        atm_oi_ratio = _oi_ratio(snap.atm_ce_oi, snap.atm_pe_oi)
        near_atm_oi_ratio = _near_atm_oi_ratio(snap)

        return {
            "ret_1m": ret_1m,
            "ret_3m": ret_3m,
            "ret_5m": ret_5m,
            "ema_9_21_spread": (
                float(self._ema_9 - self._ema_21)
                if (self._ema_9 is not None and self._ema_21 is not None)
                else None
            ),
            "ema_9_slope": (float(self._ema_9 - prev_ema_9) if (self._ema_9 is not None and prev_ema_9 is not None) else None),
            "ema_21_slope": (float(self._ema_21 - prev_ema_21) if (self._ema_21 is not None and prev_ema_21 is not None) else None),
            "ema_50_slope": (float(self._ema_50 - prev_ema_50) if (self._ema_50 is not None and prev_ema_50 is not None) else None),
            "rsi_14": rsi_14,
            "atr_ratio": atr_ratio,
            "atr_daily_percentile": atr_daily_percentile,
            "vwap_distance": (float((close - vwap) / vwap) if (vwap is not None and vwap != 0.0) else None),
            "distance_from_day_high": (
                float((close - self._day_high) / self._day_high)
                if (self._day_high is not None and self._day_high != 0.0)
                else None
            ),
            "distance_from_day_low": (
                float((close - self._day_low) / self._day_low)
                if (self._day_low is not None and self._day_low != 0.0)
                else None
            ),
            "opening_range_breakout_up": (1.0 if snap.orh_broken else 0.0),
            "opening_range_breakout_down": (1.0 if snap.orl_broken else 0.0),
            "opening_range_ready": (1.0 if (snap.or_ready or (snap.minutes_since_open is not None and int(snap.minutes_since_open) >= 15)) else 0.0),
            "minute_of_day": minute_of_day,
            "day_of_week": day_of_week,
            "fut_rel_volume_20": fut_rel_volume_20,
            "fut_volume_accel_1m": fut_volume_accel_1m,
            "fut_oi_change_1m": fut_oi_change_1m,
            "fut_oi_change_5m": fut_oi_change_5m,
            "fut_oi_rel_20": fut_oi_rel_20,
            "fut_oi_zscore_20": fut_oi_zscore_20,
            "basis": None,
            "basis_change_1m": None,
            "dte_days": (_to_float(snap.days_to_expiry)),
            "is_expiry_day": (1.0 if snap.is_expiry_day else 0.0),
            "is_near_expiry": is_near_expiry,
            "vix_prev_close": _to_float(snap.vix_prev_close),
            "vix_prev_close_change_1d": None,
            "vix_prev_close_zscore_20d": None,
            "is_high_vix_day": high_vix_day,
            "regime_vol_high": regime_vol_high,
            "regime_vol_low": regime_vol_low,
            "regime_vol_neutral": regime_vol_neutral,
            "regime_atr_high": 1.0 if (atr_daily_percentile is not None and atr_daily_percentile >= ATR_PERCENTILE_HIGH) else 0.0,
            "regime_atr_low": 1.0 if (atr_daily_percentile is not None and atr_daily_percentile <= ATR_PERCENTILE_LOW) else 0.0,
            "regime_trend_up": trend_up,
            "regime_trend_down": trend_down,
            "regime_expiry_near": is_near_expiry,
            "pcr_oi": pcr,
            "pcr_change_5m": pcr_change_5m,
            "pcr_change_15m": pcr_change_15m,
            "ce_pe_oi_diff": (
                float((snap.total_ce_oi or 0.0) - (snap.total_pe_oi or 0.0))
                if (snap.total_ce_oi is not None or snap.total_pe_oi is not None)
                else None
            ),
            "ce_pe_volume_diff": (
                float((snap.atm_ce_volume or 0.0) - (snap.atm_pe_volume or 0.0))
                if (snap.atm_ce_volume is not None or snap.atm_pe_volume is not None)
                else None
            ),
            "options_volume_total": atm_total_vol,
            "options_rel_volume_20": options_rel_volume_20,
            "atm_call_return_1m": _pct_change(
                self._atm_ce_closes[-1] if len(self._atm_ce_closes) >= 1 else None,
                self._atm_ce_closes[-2] if len(self._atm_ce_closes) >= 2 else None,
            ),
            "atm_put_return_1m": _pct_change(
                self._atm_pe_closes[-1] if len(self._atm_pe_closes) >= 1 else None,
                self._atm_pe_closes[-2] if len(self._atm_pe_closes) >= 2 else None,
            ),
            "atm_oi_change_1m": (
                atm_oi_change_1m
            ),
            "atm_oi_ratio": atm_oi_ratio,
            "near_atm_oi_ratio": near_atm_oi_ratio,
            "atm_iv": (
                float(((snap.atm_ce_iv or 0.0) + (snap.atm_pe_iv or 0.0)) / 2.0)
                if (snap.atm_ce_iv is not None and snap.atm_pe_iv is not None)
                else None
            ),
            "iv_skew": _to_float(snap.iv_skew),
        }

    def _prev_atm_oi_sum(self, snap: SnapshotAccessor) -> Optional[float]:
        current = None
        if snap.atm_ce_oi is not None or snap.atm_pe_oi is not None:
            current = float((snap.atm_ce_oi or 0.0) + (snap.atm_pe_oi or 0.0))
        prev = self._last_atm_oi_sum
        self._last_atm_oi_sum = current
        return _to_float(prev)
