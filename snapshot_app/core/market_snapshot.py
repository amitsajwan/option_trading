import argparse
import json
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
import requests
from contracts_app import TimestampSourceMode, isoformat_ist
from .market_snapshot_contract import SCHEMA_NAME, SCHEMA_VERSION

try:
    from .greeks_calculator import GreeksCalculator
except Exception:  # pragma: no cover - runtime dependency
    GreeksCalculator = None


IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN_MINUTE = 9 * 60 + 15
SESSION_CLOSE_MINUTE = 15 * 60 + 30
OPTION_PRICE_HISTORY_MAXLEN = 4_000


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _credentials_candidates() -> List[Path]:
    out: List[Path] = []
    configured = str(os.getenv("KITE_CREDENTIALS_PATH") or "").strip()
    if configured:
        out.append(Path(configured))
    out.append(Path.cwd() / "credentials.json")
    out.append(Path(__file__).resolve().parents[2] / "credentials.json")
    seen: set[str] = set()
    uniq: List[Path] = []
    for path in out:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)
    return uniq


def _load_kite_credentials() -> tuple[Optional[str], Optional[str]]:
    api_key = str(os.getenv("KITE_API_KEY") or "").strip()
    access_token = str(os.getenv("KITE_ACCESS_TOKEN") or "").strip()
    if api_key and access_token:
        return api_key, access_token

    for path in _credentials_candidates():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        key = str(payload.get("api_key") or "").strip()
        token = str(
            payload.get("access_token")
            or ((payload.get("data") or {}).get("access_token") if isinstance(payload.get("data"), dict) else "")
            or ""
        ).strip()
        if key and token:
            return key, token
    return None, None


def _build_kite_client(api_key: str, access_token: str) -> Any:
    from kiteconnect import KiteConnect

    client = KiteConnect(api_key=api_key, timeout=int(os.getenv("KITE_HTTP_TIMEOUT", "30")))
    client.set_access_token(access_token)
    return client


def _extract_underlying_symbol(contract_symbol: str) -> str:
    symbol = str(contract_symbol or "").strip().upper()
    match = re.match(r"^([A-Z]+)\d", symbol)
    if match:
        return match.group(1)
    if symbol.endswith("-I"):
        return symbol[:-2]
    for suffix in ("FUT", "CE", "PE"):
        if suffix in symbol:
            left = symbol.split(suffix)[0]
            left = re.sub(r"\d+", "", left)
            return left
    return re.sub(r"[^A-Z]", "", symbol)


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _nullable_float(value: Any) -> Optional[float]:
    out = _safe_float(value)
    if np.isfinite(out):
        return float(out)
    return None


def _nullable_int(value: Any) -> Optional[int]:
    out = _safe_float(value)
    if np.isfinite(out):
        return int(round(float(out)))
    return None


def _to_ist_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"unable to parse timestamp: {value}")
    if ts.tzinfo is None:
        return pd.Timestamp(ts)
    return pd.Timestamp(ts.tz_convert(IST).tz_localize(None))


def _coerce_ist_timestamp(value: Any) -> Any:
    try:
        return _to_ist_timestamp(value)
    except Exception:
        return pd.NaT


def _option_expiry_date(chain_expiry: Any, trade_date: pd.Timestamp) -> pd.Timestamp:
    raw = str(chain_expiry or "").strip()
    if raw:
        for fmt in ("%Y-%m-%d", "%d%b%y", "%Y%m%d"):
            try:
                dt = datetime.strptime(raw.upper(), fmt)
                return pd.Timestamp(dt.date())
            except Exception:
                continue
        try:
            ts = pd.to_datetime(raw, errors="coerce")
            if pd.notna(ts):
                if ts.tzinfo is not None:
                    ts = ts.tz_convert(IST).tz_localize(None)
                return pd.Timestamp(ts.date())
        except Exception:
            pass

    wd = int(trade_date.dayofweek)
    delta = (3 - wd) % 7
    return pd.Timestamp((trade_date + pd.Timedelta(days=int(delta))).date())


def _session_phase(ts: pd.Timestamp) -> str:
    minute = int(ts.hour * 60 + ts.minute)
    open_min = 9 * 60 + 15
    discover_end = 9 * 60 + 45
    active_end = 14 * 60 + 30
    close_end = 15 * 60 + 30
    if open_min <= minute < discover_end:
        return "DISCOVERY"
    if discover_end <= minute < active_end:
        return "ACTIVE"
    if active_end <= minute <= close_end:
        return "PRE_CLOSE"
    return "CLOSED"


def _minutes_since_open(ts: pd.Timestamp) -> Optional[int]:
    open_ts = ts.normalize() + pd.Timedelta(hours=9, minutes=15)
    mins = int((ts - open_ts) / pd.Timedelta(minutes=1))
    if mins < 0:
        return None
    return mins


def _minutes_to_close(ts: pd.Timestamp) -> int:
    minute = int(ts.hour * 60 + ts.minute)
    return max(0, int(SESSION_CLOSE_MINUTE - minute))


def _normalize_ohlc_frame(ohlc: pd.DataFrame) -> pd.DataFrame:
    if ohlc is None or len(ohlc) == 0:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    out = ohlc.copy()
    if "timestamp" not in out.columns:
        if "start_at" in out.columns:
            out["timestamp"] = out["start_at"]
        else:
            raise ValueError("ohlc frame missing timestamp/start_at")
    out["timestamp"] = out["timestamp"].map(_coerce_ist_timestamp)
    for col in ("open", "high", "low", "close", "volume", "oi"):
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return out


def _merge_ohlc_history(primary: pd.DataFrame, supplemental: pd.DataFrame) -> pd.DataFrame:
    if supplemental is None or len(supplemental) == 0:
        return _normalize_ohlc_frame(primary)
    if primary is None or len(primary) == 0:
        return _normalize_ohlc_frame(supplemental)
    merged = pd.concat([supplemental, primary], ignore_index=True)
    merged = _normalize_ohlc_frame(merged)
    merged = merged.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp").reset_index(drop=True)
    return merged


def _extract_chain_strikes(chain: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_strikes = chain.get("strikes")
    if not isinstance(raw_strikes, list):
        return []

    out: List[Dict[str, Any]] = []
    for row in raw_strikes:
        if not isinstance(row, dict):
            continue
        strike = _safe_float(row.get("strike"))
        if not np.isfinite(strike):
            continue

        # v2.0 contract: strike rows carry flat fields with optional OHLC.
        ce_ltp = _safe_float(row.get("ce_ltp"))
        pe_ltp = _safe_float(row.get("pe_ltp"))
        ce_oi = _safe_float(row.get("ce_oi"))
        pe_oi = _safe_float(row.get("pe_oi"))
        ce_vol = _safe_float(row.get("ce_volume"))
        pe_vol = _safe_float(row.get("pe_volume"))
        ce_iv = _safe_float(row.get("ce_iv"))
        pe_iv = _safe_float(row.get("pe_iv"))
        ce_open = _safe_float(row.get("ce_open"))
        ce_high = _safe_float(row.get("ce_high"))
        ce_low = _safe_float(row.get("ce_low"))
        pe_open = _safe_float(row.get("pe_open"))
        pe_high = _safe_float(row.get("pe_high"))
        pe_low = _safe_float(row.get("pe_low"))

        out.append(
            {
                "strike": float(strike),
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_volume": ce_vol,
                "pe_volume": pe_vol,
                "ce_iv": ce_iv,
                "pe_iv": pe_iv,
                "ce_open": ce_open,
                "ce_high": ce_high,
                "ce_low": ce_low,
                "pe_open": pe_open,
                "pe_high": pe_high,
                "pe_low": pe_low,
            }
        )
    return out


def _nearest_strike(strikes: List[Dict[str, Any]], fut_close: float) -> Optional[float]:
    if not strikes or not np.isfinite(fut_close):
        return None
    values = [float(x["strike"]) for x in strikes if np.isfinite(_safe_float(x.get("strike")))]
    if not values:
        return None
    return float(min(values, key=lambda x: abs(x - fut_close)))


def _same_atm_strike(item: Dict[str, Any], atm_strike: Optional[float]) -> bool:
    if atm_strike is None:
        return True
    item_strike = _safe_float(item.get("atm_strike"))
    if not np.isfinite(item_strike):
        return False
    return int(round(item_strike)) == int(round(float(atm_strike)))


def _find_history_30m(
    history: Deque[Dict[str, Any]],
    current_ts: pd.Timestamp,
    *,
    atm_strike: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    target = current_ts - pd.Timedelta(minutes=30)
    for item in reversed(history):
        ts = item.get("timestamp")
        if not isinstance(ts, pd.Timestamp):
            continue
        if not _same_atm_strike(item, atm_strike):
            continue
        if ts <= target:
            return item
    return None


def _find_recent_history(
    history: Deque[Dict[str, Any]],
    current_ts: pd.Timestamp,
    *,
    max_lookback_minutes: int,
    atm_strike: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    threshold = current_ts - pd.Timedelta(minutes=max(1, int(max_lookback_minutes)))
    for item in reversed(history):
        ts = item.get("timestamp")
        if not isinstance(ts, pd.Timestamp):
            continue
        if not _same_atm_strike(item, atm_strike):
            continue
        if ts >= current_ts:
            continue
        if ts < threshold:
            return None
        return item
    return None


def _history_mean(
    history: Deque[Dict[str, Any]],
    key: str,
    limit: int = 30,
    *,
    atm_strike: Optional[float] = None,
) -> Optional[float]:
    vals: List[float] = []
    for item in reversed(history):
        if not _same_atm_strike(item, atm_strike):
            continue
        v = _safe_float(item.get(key))
        if np.isfinite(v):
            vals.append(float(v))
        if len(vals) >= int(limit):
            break
    if not vals:
        return None
    return float(np.mean(vals))


def _ema_last(series: pd.Series, span: int) -> Optional[float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) < int(span):
        return None
    ema = values.ewm(span=int(span), adjust=False, min_periods=int(span)).mean().iloc[-1]
    return _nullable_float(ema)


def _session_vwap(day_bars: pd.DataFrame) -> Optional[float]:
    if day_bars is None or len(day_bars) == 0:
        return None

    work = day_bars.copy()
    for col in ("high", "low", "close", "volume"):
        work[col] = pd.to_numeric(work.get(col), errors="coerce")
    work = work.dropna(subset=["high", "low", "close", "volume"])
    if len(work) == 0:
        return None

    typical_price = (work["high"] + work["low"] + work["close"]) / 3.0
    volume = work["volume"].clip(lower=0.0)
    valid = typical_price.notna() & volume.notna() & (volume > 0.0)
    if not bool(valid.any()):
        return None

    cum_pv = (typical_price[valid] * volume[valid]).cumsum()
    cum_volume = volume[valid].cumsum()
    if len(cum_volume) == 0 or float(cum_volume.iloc[-1]) <= 0.0:
        return None
    return _nullable_float((cum_pv / cum_volume).iloc[-1])


def _resample_bars(bars: pd.DataFrame, *, timeframe_minutes: int) -> pd.DataFrame:
    if bars is None or len(bars) == 0:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])

    minutes = max(1, int(timeframe_minutes))
    work = bars.copy()
    work["bucket"] = work["timestamp"].dt.floor(f"{minutes}min")
    grouped = (
        work.groupby("bucket", sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            oi=("oi", "last"),
        )
        .reset_index()
        .rename(columns={"bucket": "timestamp"})
    )
    return _normalize_ohlc_frame(grouped)


def _rsi_last(series: pd.Series, period: int = 14) -> Optional[float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    p = max(2, int(period))
    if len(values) < (p + 1):
        return None
    delta = values.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=(1.0 / p), adjust=False, min_periods=p).mean()
    avg_loss = loss.ewm(alpha=(1.0 / p), adjust=False, min_periods=p).mean()
    last_gain = _safe_float(avg_gain.iloc[-1])
    last_loss = _safe_float(avg_loss.iloc[-1])
    if np.isfinite(last_gain) and np.isfinite(last_loss):
        if last_loss == 0.0:
            return 100.0 if last_gain > 0.0 else 50.0
        rs = float(last_gain / last_loss)
        return _nullable_float(100.0 - (100.0 / (1.0 + rs)))
    return None


def _atr_last(bars: pd.DataFrame, period: int = 14) -> Optional[float]:
    if bars is None or len(bars) == 0:
        return None
    p = max(2, int(period))
    high = pd.to_numeric(bars.get("high"), errors="coerce")
    low = pd.to_numeric(bars.get("low"), errors="coerce")
    close = pd.to_numeric(bars.get("close"), errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1, skipna=True)
    atr = tr.ewm(alpha=(1.0 / p), adjust=False, min_periods=p).mean()
    if len(atr) == 0:
        return None
    return _nullable_float(atr.iloc[-1])


def _atr_series(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    if bars is None or len(bars) == 0:
        return pd.Series(dtype=float)
    p = max(2, int(period))
    high = pd.to_numeric(bars.get("high"), errors="coerce")
    low = pd.to_numeric(bars.get("low"), errors="coerce")
    close = pd.to_numeric(bars.get("close"), errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1, skipna=True)
    return tr.ewm(alpha=(1.0 / p), adjust=False, min_periods=p).mean()


def _daily_atr_percentile(bars: pd.DataFrame, trade_date: pd.Timestamp, period: int = 14) -> Optional[float]:
    work = _normalize_ohlc_frame(bars)
    if len(work) == 0:
        return None
    work = work.copy()
    work["trade_date"] = work["timestamp"].dt.date.astype(str)
    work["atr_14"] = _atr_series(work, period=period)
    daily = work.groupby("trade_date", sort=True)["atr_14"].last().reset_index()
    daily = daily.dropna(subset=["atr_14"]).reset_index(drop=True)
    if len(daily) == 0:
        return None
    daily["atr_daily_percentile"] = daily["atr_14"].expanding(min_periods=5).rank(pct=True)
    current = daily.loc[daily["trade_date"].astype(str) == str(pd.Timestamp(trade_date).date()), "atr_daily_percentile"]
    if len(current) == 0:
        return None
    return _nullable_float(current.iloc[-1])


def _bars_since_first_true(condition: pd.Series) -> Optional[int]:
    if condition is None or len(condition) == 0:
        return None
    valid = condition.fillna(False).astype(bool)
    if not bool(valid.any()):
        return None
    first_idx = int(np.flatnonzero(valid.to_numpy())[0])
    last_idx = len(valid) - 1
    return max(0, int(last_idx - first_idx))


def _ladder_aggregates(
    strikes: List[Dict[str, Any]],
    *,
    atm_strike: Optional[float],
    total_ce_oi: float,
    total_pe_oi: float,
    total_ce_volume: float,
    total_pe_volume: float,
) -> Dict[str, Any]:
    out = {
        "near_atm_pcr": None,
        "near_atm_oi_concentration": None,
        "near_atm_volume_concentration": None,
        "oi_sum_m3_p3_ce": None,
        "oi_sum_m3_p3_pe": None,
        "vol_sum_m3_p3_ce": None,
        "vol_sum_m3_p3_pe": None,
    }
    if not strikes or atm_strike is None:
        return out

    ordered = [
        row
        for row in sorted(
            strikes,
            key=lambda item: _safe_float(item.get("strike")),
        )
        if np.isfinite(_safe_float(row.get("strike")))
    ]
    if not ordered:
        return out

    atm_idx = min(
        range(len(ordered)),
        key=lambda idx: abs(_safe_float(ordered[idx].get("strike")) - float(atm_strike)),
    )
    start = max(0, int(atm_idx - 3))
    end = min(len(ordered), int(atm_idx + 4))
    window = ordered[start:end]
    ce_oi_sum = float(np.nansum([_safe_float(item.get("ce_oi")) for item in window]))
    pe_oi_sum = float(np.nansum([_safe_float(item.get("pe_oi")) for item in window]))
    ce_vol_sum = float(np.nansum([_safe_float(item.get("ce_volume")) for item in window]))
    pe_vol_sum = float(np.nansum([_safe_float(item.get("pe_volume")) for item in window]))

    near_atm_pcr = None
    if np.isfinite(ce_oi_sum) and ce_oi_sum > 0.0 and np.isfinite(pe_oi_sum):
        near_atm_pcr = float(pe_oi_sum / ce_oi_sum)

    oi_concentration = None
    total_oi = (
        float(total_ce_oi) + float(total_pe_oi)
        if np.isfinite(total_ce_oi) and np.isfinite(total_pe_oi)
        else float("nan")
    )
    if np.isfinite(total_oi) and total_oi > 0.0:
        oi_concentration = float((ce_oi_sum + pe_oi_sum) / total_oi)

    volume_concentration = None
    total_volume = (
        float(total_ce_volume) + float(total_pe_volume)
        if np.isfinite(total_ce_volume) and np.isfinite(total_pe_volume)
        else float("nan")
    )
    if np.isfinite(total_volume) and total_volume > 0.0:
        volume_concentration = float((ce_vol_sum + pe_vol_sum) / total_volume)

    out.update(
        {
            "near_atm_pcr": _nullable_float(near_atm_pcr),
            "near_atm_oi_concentration": _nullable_float(oi_concentration),
            "near_atm_volume_concentration": _nullable_float(volume_concentration),
            "oi_sum_m3_p3_ce": _nullable_float(ce_oi_sum),
            "oi_sum_m3_p3_pe": _nullable_float(pe_oi_sum),
            "vol_sum_m3_p3_ce": _nullable_float(ce_vol_sum),
            "vol_sum_m3_p3_pe": _nullable_float(pe_vol_sum),
        }
    )
    return out


def _macd_last(
    series: pd.Series,
    *,
    fast_span: int = 12,
    slow_span: int = 26,
    signal_span: int = 9,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    fast = max(2, int(fast_span))
    slow = max(fast + 1, int(slow_span))
    signal = max(2, int(signal_span))
    if len(values) < (slow + signal):
        return None, None, None
    ema_fast = values.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = values.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    macd_hist = macd_line - macd_signal
    return (
        _nullable_float(macd_line.iloc[-1]),
        _nullable_float(macd_signal.iloc[-1]),
        _nullable_float(macd_hist.iloc[-1]),
    )


def _bollinger_last(series: pd.Series, period: int = 20, n_std: float = 2.0) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    p = max(2, int(period))
    if len(values) < p:
        return None, None, None, None
    ma = values.rolling(window=p, min_periods=p).mean()
    std = values.rolling(window=p, min_periods=p).std(ddof=0)
    center = _safe_float(ma.iloc[-1])
    width_std = _safe_float(std.iloc[-1])
    last_close = _safe_float(values.iloc[-1])
    if not (np.isfinite(center) and np.isfinite(width_std)):
        return None, None, None, None
    upper = float(center + float(n_std) * width_std)
    lower = float(center - float(n_std) * width_std)
    band_width = float((upper - lower) / center) if center != 0.0 else float("nan")
    pct_b = float((last_close - lower) / (upper - lower)) if (upper > lower and np.isfinite(last_close)) else float("nan")
    return _nullable_float(upper), _nullable_float(lower), _nullable_float(band_width), _nullable_float(pct_b)


def _ema_trend_label(ema9: Optional[float], ema21: Optional[float], ema50: Optional[float]) -> str:
    e9 = _safe_float(ema9)
    e21 = _safe_float(ema21)
    e50 = _safe_float(ema50)
    if not (np.isfinite(e9) and np.isfinite(e21) and np.isfinite(e50)):
        return "MIXED"
    if e9 > e21 > e50:
        return "BULLISH"
    if e9 < e21 < e50:
        return "BEARISH"
    return "MIXED"


def _compute_mtf_block(bars: pd.DataFrame) -> Dict[str, Any]:
    bars_1m = _normalize_ohlc_frame(bars)
    bars_5m = _resample_bars(bars_1m, timeframe_minutes=5)
    bars_15m = _resample_bars(bars_1m, timeframe_minutes=15)

    rsi_14_1m = _rsi_last(bars_1m.get("close", pd.Series(dtype=float)), period=14)
    rsi_14_5m = _rsi_last(bars_5m.get("close", pd.Series(dtype=float)), period=14)
    rsi_14_15m = _rsi_last(bars_15m.get("close", pd.Series(dtype=float)), period=14)

    ema_9_5m = _ema_last(bars_5m.get("close", pd.Series(dtype=float)), span=9)
    ema_21_5m = _ema_last(bars_5m.get("close", pd.Series(dtype=float)), span=21)
    ema_50_5m = _ema_last(bars_5m.get("close", pd.Series(dtype=float)), span=50)
    ema_9_15m = _ema_last(bars_15m.get("close", pd.Series(dtype=float)), span=9)
    ema_21_15m = _ema_last(bars_15m.get("close", pd.Series(dtype=float)), span=21)
    ema_50_15m = _ema_last(bars_15m.get("close", pd.Series(dtype=float)), span=50)

    macd_line_5m, macd_signal_5m, macd_hist_5m = _macd_last(bars_5m.get("close", pd.Series(dtype=float)))

    atr_14_1m = _atr_last(bars_1m, period=14)
    atr_14_5m = _atr_last(bars_5m, period=14)
    atr_14_15m = _atr_last(bars_15m, period=14)

    bb_upper_5m, bb_lower_5m, bb_width_5m, bb_pct_b_5m = _bollinger_last(
        bars_5m.get("close", pd.Series(dtype=float)),
        period=20,
        n_std=2.0,
    )

    ema_trend_5m = _ema_trend_label(ema_9_5m, ema_21_5m, ema_50_5m)
    ema_trend_15m = _ema_trend_label(ema_9_15m, ema_21_15m, ema_50_15m)
    mtf_aligned = bool(
        (ema_trend_5m == ema_trend_15m)
        and (ema_trend_5m in {"BULLISH", "BEARISH"})
    )

    return {
        "rsi_14_1m": rsi_14_1m,
        "rsi_14_5m": rsi_14_5m,
        "rsi_14_15m": rsi_14_15m,
        "ema_9_5m": ema_9_5m,
        "ema_21_5m": ema_21_5m,
        "ema_50_5m": ema_50_5m,
        "ema_9_15m": ema_9_15m,
        "ema_21_15m": ema_21_15m,
        "ema_50_15m": ema_50_15m,
        "macd_line_5m": macd_line_5m,
        "macd_signal_5m": macd_signal_5m,
        "macd_hist_5m": macd_hist_5m,
        "atr_14_1m": atr_14_1m,
        "atr_14_5m": atr_14_5m,
        "atr_14_15m": atr_14_15m,
        "bb_upper_5m": bb_upper_5m,
        "bb_lower_5m": bb_lower_5m,
        "bb_width_5m": bb_width_5m,
        "bb_pct_b_5m": bb_pct_b_5m,
        "ema_trend_5m": ema_trend_5m,
        "ema_trend_15m": ema_trend_15m,
        "mtf_aligned": mtf_aligned,
    }


def _repo_rate_for_date(trade_date: pd.Timestamp, default_rate: float = 0.065) -> float:
    dt = pd.Timestamp(trade_date).date()
    if dt <= datetime(2021, 12, 31).date():
        return 0.04
    if dt <= datetime(2024, 12, 31).date():
        return 0.065
    return float(default_rate)


def _time_to_expiry_years(current_ts: pd.Timestamp, expiry_date: pd.Timestamp) -> float:
    expiry_dt = pd.Timestamp(expiry_date).normalize() + pd.Timedelta(hours=15, minutes=30)
    seconds = float((expiry_dt - current_ts).total_seconds())
    min_seconds = 60.0
    return max(seconds, min_seconds) / (365.0 * 24.0 * 3600.0)


def _normalize_iv(value: float) -> Optional[float]:
    if not np.isfinite(value):
        return None
    iv = float(value)
    if iv > 3.0:
        iv = iv / 100.0
    if iv <= 0.0:
        return None
    return float(iv)


def _compute_iv(
    *,
    market_price: float,
    underlying_price: float,
    strike: float,
    option_type: str,
    current_ts: pd.Timestamp,
    expiry_date: pd.Timestamp,
    risk_free_rate: float,
) -> Optional[float]:
    if GreeksCalculator is None:
        return None
    if not (np.isfinite(market_price) and market_price > 0.0):
        return None
    if not (np.isfinite(underlying_price) and underlying_price > 0.0):
        return None
    if not (np.isfinite(strike) and strike > 0.0):
        return None
    t = _time_to_expiry_years(current_ts=current_ts, expiry_date=expiry_date)
    try:
        iv = GreeksCalculator.calculate_implied_volatility(
            market_price=float(market_price),
            spot_price=float(underlying_price),
            strike=float(strike),
            time_to_expiry=float(t),
            risk_free_rate=float(risk_free_rate),
            option_type=str(option_type).upper(),
        )
    except Exception:
        return None
    if iv is None:
        return None
    return _normalize_iv(float(iv))


def _compute_max_pain(strikes: List[Dict[str, Any]]) -> Optional[int]:
    if not strikes:
        return None
    min_val = float("inf")
    best: Optional[int] = None
    for row in strikes:
        strike = _safe_float(row.get("strike"))
        if not np.isfinite(strike):
            continue
        pain = 0.0
        for other in strikes:
            sp = _safe_float(other.get("strike"))
            if not np.isfinite(sp):
                continue
            ce_oi = _safe_float(other.get("ce_oi"))
            pe_oi = _safe_float(other.get("pe_oi"))
            ce_oi = float(ce_oi) if np.isfinite(ce_oi) else 0.0
            pe_oi = float(pe_oi) if np.isfinite(pe_oi) else 0.0
            if sp < strike:
                pain += (strike - sp) * ce_oi
            elif sp > strike:
                pain += (sp - strike) * pe_oi
        if pain < min_val:
            min_val = float(pain)
            best = int(round(strike))
    return best


def _compute_vix_block(
    *,
    trade_date: pd.Timestamp,
    vix_daily: pd.DataFrame,
    vix_live_current: Optional[float],
) -> Dict[str, Any]:
    out = {
        "vix_prev_close": None,
        "vix_open": None,
        "vix_current": None,
        "vix_change_from_prev": None,
        "vix_intraday_chg": None,
        "vix_regime": None,
        "vix_spike_flag": False,
    }

    vd = pd.DataFrame()
    if vix_daily is not None and len(vix_daily) > 0:
        vd = vix_daily.copy()
        vd["trade_date"] = pd.to_datetime(vd["trade_date"], errors="coerce")
        for col in ("vix_open", "vix_high", "vix_low", "vix_close"):
            if col in vd.columns:
                vd[col] = pd.to_numeric(vd[col], errors="coerce")
        vd = vd.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)

    td = pd.Timestamp(trade_date).normalize()
    hist = vd[vd["trade_date"] < td] if len(vd) else pd.DataFrame()
    today = vd[vd["trade_date"] == td] if len(vd) else pd.DataFrame()

    prev_close = _safe_float(hist.iloc[-1]["vix_close"]) if len(hist) else float("nan")
    vix_open = _safe_float(today.iloc[-1]["vix_open"]) if len(today) else float("nan")
    vix_current = _safe_float(today.iloc[-1]["vix_close"]) if len(today) else float("nan")
    if np.isfinite(_safe_float(vix_live_current)):
        vix_current = float(vix_live_current)
    if not np.isfinite(vix_open) and np.isfinite(prev_close):
        vix_open = float(prev_close)
    if not np.isfinite(vix_current) and np.isfinite(vix_open):
        vix_current = float(vix_open)

    vix_prev_close = _nullable_float(prev_close)
    vix_open_n = _nullable_float(vix_open)
    vix_current_n = _nullable_float(vix_current)
    out["vix_prev_close"] = vix_prev_close
    out["vix_open"] = vix_open_n
    out["vix_current"] = vix_current_n

    if vix_prev_close is not None and vix_prev_close != 0.0 and vix_current_n is not None:
        out["vix_change_from_prev"] = float(((vix_current_n - vix_prev_close) / vix_prev_close) * 100.0)
    if vix_open_n is not None and vix_open_n != 0.0 and vix_current_n is not None:
        out["vix_intraday_chg"] = float(((vix_current_n - vix_open_n) / vix_open_n) * 100.0)

    if vix_current_n is not None:
        if vix_current_n < 14.0:
            out["vix_regime"] = "LOW"
        elif vix_current_n <= 20.0:
            out["vix_regime"] = "NORMAL"
        else:
            out["vix_regime"] = "ELEVATED"
    out["vix_spike_flag"] = bool(out["vix_intraday_chg"] is not None and out["vix_intraday_chg"] > 15.0)
    return out


@dataclass
class MarketSnapshotState:
    chain_history: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=4000))
    iv_history_expiry: Deque[float] = field(default_factory=lambda: deque(maxlen=30000))
    iv_history_non_expiry: Deque[float] = field(default_factory=lambda: deque(maxlen=30000))
    option_price_history: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=OPTION_PRICE_HISTORY_MAXLEN))


@dataclass
class PreparedMarketSnapshotWindow:
    bars: pd.DataFrame
    trade_date: pd.Timestamp
    trade_date_key: str
    futures_derived: pd.DataFrame
    session_levels: Dict[str, Any]


def prepare_market_snapshot_window(
    ohlc: pd.DataFrame,
    *,
    current_trade_date: Optional[pd.Timestamp] = None,
) -> PreparedMarketSnapshotWindow:
    bars = _normalize_ohlc_frame(ohlc)
    if len(bars) == 0:
        raise ValueError("cannot prepare MarketSnapshot window: empty ohlc frame")

    if current_trade_date is None:
        current_trade_date = pd.Timestamp(pd.Timestamp(bars.iloc[-1]["timestamp"]).date())
    resolved_trade_date = pd.Timestamp(current_trade_date).normalize()
    trade_date_key = str(resolved_trade_date.date())

    bars_calc = bars.copy()
    bars_calc["trade_date"] = bars_calc["timestamp"].dt.date.astype(str)
    bars_calc["minute_of_day"] = bars_calc["timestamp"].dt.hour * 60 + bars_calc["timestamp"].dt.minute
    bars_calc["ret_1m"] = bars_calc["close"].pct_change(1, fill_method=None)
    bars_calc["fut_return_1m"] = bars_calc["ret_1m"]
    bars_calc["fut_return_3m"] = bars_calc["close"].pct_change(3, fill_method=None)
    bars_calc["fut_return_5m"] = bars_calc["close"].pct_change(5, fill_method=None)
    bars_calc["fut_return_15m"] = bars_calc["close"].pct_change(15, fill_method=None)
    bars_calc["fut_return_30m"] = bars_calc["close"].pct_change(30, fill_method=None)
    bars_calc["realized_vol_30m"] = (
        bars_calc["ret_1m"].rolling(30, min_periods=30).std(ddof=1) * np.sqrt(252.0 * 375.0)
    )

    close = pd.to_numeric(bars_calc["close"], errors="coerce")
    bars_calc["ema_9"] = close.ewm(span=9, adjust=False, min_periods=9).mean()
    bars_calc["ema_21"] = close.ewm(span=21, adjust=False, min_periods=21).mean()
    bars_calc["ema_50"] = close.ewm(span=50, adjust=False, min_periods=50).mean()
    bars_calc["ema_9_slope"] = close.ewm(span=9, adjust=False).mean().diff()
    bars_calc["ema_21_slope"] = close.ewm(span=21, adjust=False).mean().diff()
    bars_calc["ema_50_slope"] = close.ewm(span=50, adjust=False).mean().diff()
    bars_calc["atr_14"] = _atr_series(bars_calc, period=14)

    same_day = bars_calc[bars_calc["trade_date"] == trade_date_key].copy()
    if len(same_day) == 0:
        raise ValueError(f"cannot prepare MarketSnapshot window: no bars for trade_date={trade_date_key}")

    rv_profile = (
        bars_calc[bars_calc["trade_date"] < trade_date_key]
        .groupby("minute_of_day", sort=False)["realized_vol_30m"]
        .mean()
    )
    same_day["vol_baseline"] = same_day["minute_of_day"].map(rv_profile)
    same_day["vol_baseline_fallback"] = same_day["realized_vol_30m"].expanding(min_periods=20).mean()
    invalid_vol_baseline = same_day["vol_baseline"].isna() | (same_day["vol_baseline"] <= 0.0)
    same_day.loc[invalid_vol_baseline, "vol_baseline"] = same_day.loc[invalid_vol_baseline, "vol_baseline_fallback"]
    same_day["vol_ratio"] = same_day["realized_vol_30m"] / same_day["vol_baseline"].replace(0.0, np.nan)

    vol_profile = (
        bars_calc[bars_calc["trade_date"] < trade_date_key]
        .groupby("minute_of_day", sort=False)["volume"]
        .mean()
    )
    same_day["vol_ref"] = same_day["minute_of_day"].map(vol_profile)
    same_day["vol_ref_fallback"] = same_day["volume"].rolling(30, min_periods=10).mean()
    invalid_vol_ref = same_day["vol_ref"].isna() | (same_day["vol_ref"] <= 0.0)
    same_day.loc[invalid_vol_ref, "vol_ref"] = same_day.loc[invalid_vol_ref, "vol_ref_fallback"]
    same_day["fut_volume_ratio"] = same_day["volume"] / same_day["vol_ref"].replace(0.0, np.nan)

    oi_shift_30 = pd.to_numeric(bars_calc["oi"], errors="coerce").shift(30)
    same_day["fut_oi_change_30m"] = (
        pd.to_numeric(same_day["oi"], errors="coerce") - oi_shift_30.reindex(same_day.index)
    )

    typical_price = (same_day["high"] + same_day["low"] + same_day["close"]) / 3.0
    volume = pd.to_numeric(same_day["volume"], errors="coerce").clip(lower=0.0)
    valid_vwap = typical_price.notna() & volume.notna() & (volume > 0.0)
    cumulative_volume = volume.where(valid_vwap, 0.0).cumsum()
    cumulative_pv = (typical_price.where(valid_vwap, 0.0) * volume.where(valid_vwap, 0.0)).cumsum()
    same_day["vwap"] = np.nan
    positive_volume = cumulative_volume > 0.0
    same_day.loc[positive_volume, "vwap"] = cumulative_pv.loc[positive_volume] / cumulative_volume.loc[positive_volume]
    same_day["price_vs_vwap"] = (same_day["close"] - same_day["vwap"]) / same_day["vwap"].replace(0.0, np.nan)

    same_day["atr_ratio"] = same_day["atr_14"] / same_day["close"].replace(0.0, np.nan)
    atr_daily_percentile = _daily_atr_percentile(bars_calc, resolved_trade_date, period=14)
    same_day["atr_daily_percentile"] = atr_daily_percentile

    same_day["day_high"] = same_day["high"].cummax()
    same_day["day_low"] = same_day["low"].cummin()
    same_day["dist_from_day_high"] = (same_day["close"] - same_day["day_high"]) / same_day["day_high"].replace(0.0, np.nan)
    same_day["dist_from_day_low"] = (same_day["close"] - same_day["day_low"]) / same_day["day_low"].replace(0.0, np.nan)

    by_day = {}
    for day_key, grp in bars_calc.groupby("trade_date", sort=True):
        by_day[str(day_key)] = grp.sort_values("timestamp")
    day_keys = sorted(by_day.keys())
    prev_day_key = None
    for day_key in day_keys:
        if day_key < trade_date_key:
            prev_day_key = day_key
        else:
            break

    prev_day_high = prev_day_low = prev_day_close = None
    week_high = week_low = overnight_gap = None
    if prev_day_key is not None:
        prev_df = by_day[prev_day_key]
        prev_day_high = _nullable_float(prev_df["high"].max())
        prev_day_low = _nullable_float(prev_df["low"].min())
        prev_day_close = _nullable_float(prev_df["close"].iloc[-1])
        prev_window_days = [day_key for day_key in day_keys if day_key < trade_date_key][-5:]
        if prev_window_days:
            window = pd.concat([by_day[day_key] for day_key in prev_window_days], ignore_index=True)
            week_high = _nullable_float(window["high"].max())
            week_low = _nullable_float(window["low"].min())
        today_df = by_day.get(trade_date_key)
        today_open = _safe_float(today_df["open"].iloc[0]) if today_df is not None and len(today_df) else float("nan")
        if prev_day_close is not None and np.isfinite(today_open) and prev_day_close != 0.0:
            overnight_gap = float((today_open - prev_day_close) / prev_day_close)

    futures_derived = same_day.loc[
        :,
        [
            "fut_return_1m",
            "fut_return_3m",
            "fut_return_5m",
            "fut_return_15m",
            "fut_return_30m",
            "realized_vol_30m",
            "vol_ratio",
            "fut_volume_ratio",
            "fut_oi_change_30m",
            "ema_9",
            "ema_21",
            "ema_50",
            "ema_9_slope",
            "ema_21_slope",
            "ema_50_slope",
            "vwap",
            "price_vs_vwap",
            "atr_ratio",
            "atr_daily_percentile",
            "dist_from_day_high",
            "dist_from_day_low",
        ],
    ].copy()

    session_levels = {
        "prev_day_high": prev_day_high,
        "prev_day_low": prev_day_low,
        "prev_day_close": prev_day_close,
        "week_high": week_high,
        "week_low": week_low,
        "overnight_gap": _nullable_float(overnight_gap),
    }

    return PreparedMarketSnapshotWindow(
        bars=bars,
        trade_date=resolved_trade_date,
        trade_date_key=trade_date_key,
        futures_derived=futures_derived,
        session_levels=session_levels,
    )

def build_market_snapshot(
    *,
    instrument: str,
    ohlc: pd.DataFrame,
    chain: Dict[str, Any],
    state: Optional[MarketSnapshotState] = None,
    vix_daily: Optional[pd.DataFrame] = None,
    vix_live_current: Optional[float] = None,
    prev_session_chain_baseline: Optional[Dict[str, Any]] = None,
    risk_free_rate_default: float = 0.065,
    prepared_window: Optional[PreparedMarketSnapshotWindow] = None,
    current_index: Optional[int] = None,
) -> Dict[str, Any]:
    if state is None:
        state = MarketSnapshotState()

    if prepared_window is None:
        bars = _normalize_ohlc_frame(ohlc)
        if len(bars) == 0:
            raise ValueError("cannot build MarketSnapshot: empty ohlc frame")
        resolved_index = int(len(bars) - 1 if current_index is None else current_index)
        if resolved_index < 0 or resolved_index >= len(bars):
            raise IndexError(
                f"current_index out of range for prepared MarketSnapshot window: {resolved_index}"
            )
        prepared_window = prepare_market_snapshot_window(
            bars,
            current_trade_date=pd.Timestamp(pd.Timestamp(bars.iloc[resolved_index]["timestamp"]).date()),
        )
    else:
        bars = prepared_window.bars
        if len(bars) == 0:
            raise ValueError("cannot build MarketSnapshot: empty prepared MarketSnapshot window")
        resolved_index = int(len(bars) - 1 if current_index is None else current_index)
        if resolved_index < 0 or resolved_index >= len(bars):
            raise IndexError(
                f"current_index out of range for prepared MarketSnapshot window: {resolved_index}"
            )

    latest = bars.iloc[resolved_index]
    ts = pd.Timestamp(latest["timestamp"])
    trade_date = pd.Timestamp(ts.date())
    minute_of_day = int(ts.hour * 60 + ts.minute)
    snapshot_id = ts.strftime("%Y%m%d_%H%M")

    if prepared_window.trade_date_key != str(trade_date.date()) or resolved_index not in prepared_window.futures_derived.index:
        prefix_bars = bars.iloc[: resolved_index + 1].copy()
        prepared_window = prepare_market_snapshot_window(
            prefix_bars,
            current_trade_date=trade_date,
        )
        bars = prepared_window.bars
        resolved_index = len(bars) - 1
        latest = bars.iloc[resolved_index]
        ts = pd.Timestamp(latest["timestamp"])
        trade_date = pd.Timestamp(ts.date())
        minute_of_day = int(ts.hour * 60 + ts.minute)
        snapshot_id = ts.strftime("%Y%m%d_%H%M")

    fut_close = _safe_float(latest.get("close"))
    strikes = _extract_chain_strikes(chain)
    atm_strike = _nearest_strike(strikes=strikes, fut_close=fut_close)
    atm_row = None
    if atm_strike is not None:
        for row in strikes:
            if int(round(_safe_float(row.get("strike")))) == int(round(atm_strike)):
                atm_row = row
                break

    expiry_date = _option_expiry_date(chain.get("expiry"), trade_date=trade_date)
    dte_days = int((expiry_date.normalize() - trade_date.normalize()).days)
    dte_days = max(dte_days, 0)
    minutes_since_open = _minutes_since_open(ts)
    minutes_to_close = _minutes_to_close(ts)
    prefix_bars = bars.iloc[: resolved_index + 1].copy()
    mss1 = {
        "snapshot_id": snapshot_id,
        "timestamp": isoformat_ist(ts.to_pydatetime(), naive_mode=TimestampSourceMode.MARKET_IST),
        "date": str(trade_date.date()),
        "time": ts.strftime("%H:%M:%S"),
        "minutes_since_open": minutes_since_open,
        "minutes_to_close": minutes_to_close,
        "day_of_week": int(ts.dayofweek),
        "days_to_expiry": dte_days,
        "is_expiry_day": bool(dte_days == 0),
        "session_phase": _session_phase(ts),
        "is_first_hour": bool(minutes_since_open is not None and minutes_since_open < 60),
        "is_last_hour": bool(minutes_to_close <= 60),
    }

    mss2 = {
        "fut_open": _nullable_float(latest.get("open")),
        "fut_high": _nullable_float(latest.get("high")),
        "fut_low": _nullable_float(latest.get("low")),
        "fut_close": _nullable_float(fut_close),
        "fut_volume": _nullable_int(latest.get("volume")),
        "fut_oi": _nullable_int(latest.get("oi")),
    }

    bars_calc = prefix_bars.copy()
    bars_calc["trade_date"] = bars_calc["timestamp"].dt.date.astype(str)
    bars_calc["minute_of_day"] = bars_calc["timestamp"].dt.hour * 60 + bars_calc["timestamp"].dt.minute
    same_day = bars_calc[bars_calc["trade_date"] == str(trade_date.date())].copy()
    current_futures = prepared_window.futures_derived.loc[resolved_index]

    mss3 = {
        "fut_return_1m": _nullable_float(current_futures.get("fut_return_1m")),
        "fut_return_3m": _nullable_float(current_futures.get("fut_return_3m")),
        "fut_return_5m": _nullable_float(current_futures.get("fut_return_5m")),
        "fut_return_15m": _nullable_float(current_futures.get("fut_return_15m")),
        "fut_return_30m": _nullable_float(current_futures.get("fut_return_30m")),
        "realized_vol_30m": _nullable_float(current_futures.get("realized_vol_30m")),
        "vol_ratio": _nullable_float(current_futures.get("vol_ratio")),
        "fut_volume_ratio": _nullable_float(current_futures.get("fut_volume_ratio")),
        "fut_oi_change_30m": _nullable_int(current_futures.get("fut_oi_change_30m")),
        "ema_9": _nullable_float(current_futures.get("ema_9")),
        "ema_21": _nullable_float(current_futures.get("ema_21")),
        "ema_50": _nullable_float(current_futures.get("ema_50")),
        "ema_9_slope": _nullable_float(current_futures.get("ema_9_slope")),
        "ema_21_slope": _nullable_float(current_futures.get("ema_21_slope")),
        "ema_50_slope": _nullable_float(current_futures.get("ema_50_slope")),
        "vwap": _nullable_float(current_futures.get("vwap")),
        "price_vs_vwap": _nullable_float(current_futures.get("price_vs_vwap")),
        "atr_ratio": _nullable_float(current_futures.get("atr_ratio")),
        "atr_daily_percentile": _nullable_float(current_futures.get("atr_daily_percentile")),
        "dist_from_day_high": _nullable_float(current_futures.get("dist_from_day_high")),
        "dist_from_day_low": _nullable_float(current_futures.get("dist_from_day_low")),
    }
    mss_mtf = _compute_mtf_block(bars_calc)

    day_bars = bars_calc[bars_calc["trade_date"] == str(trade_date.date())].copy()
    open_window = day_bars[
        (day_bars["timestamp"].dt.hour == 9)
        & (day_bars["timestamp"].dt.minute >= 15)
        & (day_bars["timestamp"].dt.minute < 30)
    ]
    orh = _safe_float(open_window["high"].max()) if len(open_window) else float("nan")
    orl = _safe_float(open_window["low"].min()) if len(open_window) else float("nan")
    or_width = float(orh - orl) if np.isfinite(orh) and np.isfinite(orl) else float("nan")
    or_width_pct = (
        float(or_width / fut_close)
        if np.isfinite(or_width) and np.isfinite(fut_close) and fut_close != 0.0
        else float("nan")
    )
    price_vs_orh = float((fut_close - orh) / orh) if np.isfinite(fut_close) and np.isfinite(orh) and orh != 0 else float("nan")
    price_vs_orl = float((fut_close - orl) / orl) if np.isfinite(fut_close) and np.isfinite(orl) and orl != 0 else float("nan")

    five = day_bars.copy()
    five["bucket"] = five["timestamp"].dt.floor("5min")
    close_5m = five.groupby("bucket", sort=True)["close"].last().reset_index()
    close_after_or = close_5m[
        close_5m["bucket"] >= (ts.normalize() + pd.Timedelta(hours=9, minutes=30))
    ]
    orh_broken = bool(np.isfinite(orh) and len(close_after_or) and (close_after_or["close"] > orh).any())
    orl_broken = bool(np.isfinite(orl) and len(close_after_or) and (close_after_or["close"] < orl).any())
    breakout_up_series = pd.Series(False, index=day_bars.index)
    breakout_down_series = pd.Series(False, index=day_bars.index)
    if np.isfinite(orh):
        breakout_up_series = day_bars["close"] > orh
    if np.isfinite(orl):
        breakout_down_series = day_bars["close"] < orl
    bars_since_or_break_up = _bars_since_first_true(breakout_up_series)
    bars_since_or_break_down = _bars_since_first_true(breakout_down_series)
    mss4 = {
        "orh": _nullable_float(orh),
        "orl": _nullable_float(orl),
        "or_width": _nullable_float(or_width),
        "or_width_pct": _nullable_float(or_width_pct),
        "price_vs_orh": _nullable_float(price_vs_orh),
        "price_vs_orl": _nullable_float(price_vs_orl),
        "opening_range_ready": bool(minutes_since_open is not None and minutes_since_open >= 15),
        "orh_broken": orh_broken,
        "orl_broken": orl_broken,
        "bars_since_or_break_up": _nullable_int(bars_since_or_break_up),
        "bars_since_or_break_down": _nullable_int(bars_since_or_break_down),
    }

    mss5 = _compute_vix_block(
        trade_date=trade_date,
        vix_daily=(vix_daily if vix_daily is not None else pd.DataFrame()),
        vix_live_current=vix_live_current,
    )

    ce_ltp = _safe_float(atm_row.get("ce_ltp")) if isinstance(atm_row, dict) else float("nan")
    pe_ltp = _safe_float(atm_row.get("pe_ltp")) if isinstance(atm_row, dict) else float("nan")
    ce_open = _safe_float(atm_row.get("ce_open")) if isinstance(atm_row, dict) else float("nan")
    ce_high = _safe_float(atm_row.get("ce_high")) if isinstance(atm_row, dict) else float("nan")
    ce_low = _safe_float(atm_row.get("ce_low")) if isinstance(atm_row, dict) else float("nan")
    pe_open = _safe_float(atm_row.get("pe_open")) if isinstance(atm_row, dict) else float("nan")
    pe_high = _safe_float(atm_row.get("pe_high")) if isinstance(atm_row, dict) else float("nan")
    pe_low = _safe_float(atm_row.get("pe_low")) if isinstance(atm_row, dict) else float("nan")
    ce_oi = _safe_float(atm_row.get("ce_oi")) if isinstance(atm_row, dict) else float("nan")
    pe_oi = _safe_float(atm_row.get("pe_oi")) if isinstance(atm_row, dict) else float("nan")
    ce_vol = _safe_float(atm_row.get("ce_volume")) if isinstance(atm_row, dict) else float("nan")
    pe_vol = _safe_float(atm_row.get("pe_volume")) if isinstance(atm_row, dict) else float("nan")

    total_ce_oi = float(np.nansum([_safe_float(x.get("ce_oi")) for x in strikes])) if strikes else float("nan")
    total_pe_oi = float(np.nansum([_safe_float(x.get("pe_oi")) for x in strikes])) if strikes else float("nan")
    total_ce_volume = float(np.nansum([_safe_float(x.get("ce_volume")) for x in strikes])) if strikes else float("nan")
    total_pe_volume = float(np.nansum([_safe_float(x.get("pe_volume")) for x in strikes])) if strikes else float("nan")
    pcr = _safe_float(chain.get("pcr"))
    if not np.isfinite(pcr):
        pcr = (total_pe_oi / total_ce_oi) if np.isfinite(total_ce_oi) and total_ce_oi > 0 else float("nan")
    max_pain = _nullable_int(chain.get("max_pain"))
    if max_pain is None:
        max_pain = _compute_max_pain(strikes)
    ce_oi_top_strike = None
    pe_oi_top_strike = None
    if strikes:
        ce_best = max(strikes, key=lambda x: _safe_float(x.get("ce_oi")))
        pe_best = max(strikes, key=lambda x: _safe_float(x.get("pe_oi")))
        ce_oi_top_strike = _nullable_int(ce_best.get("strike"))
        pe_oi_top_strike = _nullable_int(pe_best.get("strike"))

    prev_30 = _find_history_30m(state.chain_history, ts, atm_strike=atm_strike)
    pcr_change_30m = None
    if prev_30 is not None:
        prev_pcr = _safe_float(prev_30.get("pcr"))
        if np.isfinite(prev_pcr) and np.isfinite(pcr):
            pcr_change_30m = float(pcr - prev_pcr)
    ce_pe_oi_diff = (
        float(total_ce_oi - total_pe_oi)
        if np.isfinite(total_ce_oi) and np.isfinite(total_pe_oi)
        else float("nan")
    )
    ce_pe_volume_diff = (
        float(total_ce_volume - total_pe_volume)
        if np.isfinite(total_ce_volume) and np.isfinite(total_pe_volume)
        else float("nan")
    )
    atm_straddle_price = (
        float(ce_ltp + pe_ltp)
        if np.isfinite(_safe_float(ce_ltp)) and np.isfinite(_safe_float(pe_ltp))
        else float("nan")
    )
    atm_straddle_pct = (
        float(atm_straddle_price / fut_close)
        if np.isfinite(atm_straddle_price) and np.isfinite(fut_close) and fut_close != 0.0
        else float("nan")
    )
    distance_to_max_pain_pct = (
        float((fut_close - float(max_pain)) / fut_close)
        if max_pain is not None and np.isfinite(fut_close) and fut_close != 0.0
        else float("nan")
    )

    mss6 = {
        "atm_strike": _nullable_int(atm_strike),
        "strike_count": int(len(strikes)),
        "total_ce_oi": _nullable_int(total_ce_oi),
        "total_pe_oi": _nullable_int(total_pe_oi),
        "total_ce_volume": _nullable_int(total_ce_volume),
        "total_pe_volume": _nullable_int(total_pe_volume),
        "pcr": _nullable_float(pcr),
        "pcr_change_30m": _nullable_float(pcr_change_30m),
        "max_pain": max_pain,
        "ce_oi_top_strike": ce_oi_top_strike,
        "pe_oi_top_strike": pe_oi_top_strike,
        "ce_pe_oi_diff": _nullable_float(ce_pe_oi_diff),
        "ce_pe_volume_diff": _nullable_float(ce_pe_volume_diff),
        "atm_straddle_price": _nullable_float(atm_straddle_price),
        "atm_straddle_pct": _nullable_float(atm_straddle_pct),
        "distance_to_max_pain_pct": _nullable_float(distance_to_max_pain_pct),
    }

    ce_iv = _normalize_iv(_safe_float(atm_row.get("ce_iv"))) if isinstance(atm_row, dict) else None
    pe_iv = _normalize_iv(_safe_float(atm_row.get("pe_iv"))) if isinstance(atm_row, dict) else None
    rf = _repo_rate_for_date(trade_date=trade_date, default_rate=risk_free_rate_default)
    if ce_iv is None and atm_strike is not None:
        ce_iv = _compute_iv(
            market_price=ce_ltp,
            underlying_price=fut_close,
            strike=float(atm_strike),
            option_type="CE",
            current_ts=ts,
            expiry_date=expiry_date,
            risk_free_rate=rf,
        )
    if pe_iv is None and atm_strike is not None:
        pe_iv = _compute_iv(
            market_price=pe_ltp,
            underlying_price=fut_close,
            strike=float(atm_strike),
            option_type="PE",
            current_ts=ts,
            expiry_date=expiry_date,
            risk_free_rate=rf,
        )

    ce_oi_change_30m = None
    pe_oi_change_30m = None
    ce_vol_ratio = None
    pe_vol_ratio = None
    prev_1m = _find_recent_history(
        state.option_price_history,
        ts,
        max_lookback_minutes=2,
        atm_strike=atm_strike,
    )
    atm_ce_return_1m = None
    atm_pe_return_1m = None
    atm_ce_oi_change_1m = None
    atm_pe_oi_change_1m = None
    if prev_1m is not None and str(prev_1m.get("trade_date") or "") == str(trade_date.date()):
        prev_ce_close = _safe_float(prev_1m.get("atm_ce_close"))
        prev_pe_close = _safe_float(prev_1m.get("atm_pe_close"))
        prev_ce_oi_1m = _safe_float(prev_1m.get("atm_ce_oi"))
        prev_pe_oi_1m = _safe_float(prev_1m.get("atm_pe_oi"))
        if np.isfinite(ce_ltp) and np.isfinite(prev_ce_close) and prev_ce_close != 0.0:
            atm_ce_return_1m = float((ce_ltp - prev_ce_close) / prev_ce_close)
        if np.isfinite(pe_ltp) and np.isfinite(prev_pe_close) and prev_pe_close != 0.0:
            atm_pe_return_1m = float((pe_ltp - prev_pe_close) / prev_pe_close)
        if np.isfinite(ce_oi) and np.isfinite(prev_ce_oi_1m):
            atm_ce_oi_change_1m = float(ce_oi - prev_ce_oi_1m)
        if np.isfinite(pe_oi) and np.isfinite(prev_pe_oi_1m):
            atm_pe_oi_change_1m = float(pe_oi - prev_pe_oi_1m)
    if prev_30 is not None:
        prev_ce_oi = _safe_float(prev_30.get("atm_ce_oi"))
        prev_pe_oi = _safe_float(prev_30.get("atm_pe_oi"))
        if np.isfinite(ce_oi) and np.isfinite(prev_ce_oi):
            ce_oi_change_30m = float(ce_oi - prev_ce_oi)
        if np.isfinite(pe_oi) and np.isfinite(prev_pe_oi):
            pe_oi_change_30m = float(pe_oi - prev_pe_oi)
    ce_vol_mean = _history_mean(state.chain_history, "atm_ce_volume", limit=30, atm_strike=atm_strike)
    pe_vol_mean = _history_mean(state.chain_history, "atm_pe_volume", limit=30, atm_strike=atm_strike)
    if np.isfinite(ce_vol) and ce_vol_mean is not None and ce_vol_mean > 0:
        ce_vol_ratio = float(ce_vol / ce_vol_mean)
    if np.isfinite(pe_vol) and pe_vol_mean is not None and pe_vol_mean > 0:
        pe_vol_ratio = float(pe_vol / pe_vol_mean)
    atm_ce_pe_price_diff = (
        float(ce_ltp - pe_ltp)
        if np.isfinite(ce_ltp) and np.isfinite(pe_ltp)
        else float("nan")
    )
    atm_ce_pe_iv_diff = (
        float(ce_iv - pe_iv)
        if ce_iv is not None and pe_iv is not None
        else float("nan")
    )

    mss7 = {
        "atm_ce_strike": _nullable_int(atm_strike),
        "atm_ce_open": _nullable_float(ce_open),
        "atm_ce_high": _nullable_float(ce_high),
        "atm_ce_low": _nullable_float(ce_low),
        "atm_ce_close": _nullable_float(ce_ltp),
        "atm_ce_return_1m": _nullable_float(atm_ce_return_1m),
        "atm_ce_volume": _nullable_int(ce_vol),
        "atm_ce_oi": _nullable_int(ce_oi),
        "atm_ce_oi_change_1m": _nullable_int(atm_ce_oi_change_1m),
        "atm_ce_oi_change_30m": _nullable_int(ce_oi_change_30m),
        "atm_ce_iv": _nullable_float(ce_iv),
        "atm_ce_vol_ratio": _nullable_float(ce_vol_ratio),
        "atm_pe_strike": _nullable_int(atm_strike),
        "atm_pe_open": _nullable_float(pe_open),
        "atm_pe_high": _nullable_float(pe_high),
        "atm_pe_low": _nullable_float(pe_low),
        "atm_pe_close": _nullable_float(pe_ltp),
        "atm_pe_return_1m": _nullable_float(atm_pe_return_1m),
        "atm_pe_volume": _nullable_int(pe_vol),
        "atm_pe_oi": _nullable_int(pe_oi),
        "atm_pe_oi_change_1m": _nullable_int(atm_pe_oi_change_1m),
        "atm_pe_oi_change_30m": _nullable_int(pe_oi_change_30m),
        "atm_pe_iv": _nullable_float(pe_iv),
        "atm_pe_vol_ratio": _nullable_float(pe_vol_ratio),
        "atm_ce_pe_price_diff": _nullable_float(atm_ce_pe_price_diff),
        "atm_ce_pe_iv_diff": _nullable_float(atm_ce_pe_iv_diff),
    }

    iv_skew = None
    if ce_iv is not None and pe_iv is not None:
        iv_skew = float(ce_iv - pe_iv)
    if iv_skew is None:
        iv_skew_dir = None
    elif iv_skew < -0.005:
        iv_skew_dir = "PUT_FEAR"
    elif iv_skew > 0.005:
        iv_skew_dir = "CALL_GREED"
    else:
        iv_skew_dir = "NEUTRAL"

    expiry_type = "EXPIRY_DAY" if bool(mss1["is_expiry_day"]) else "NON_EXPIRY"
    atm_iv = None
    if ce_iv is not None and pe_iv is not None:
        atm_iv = float((ce_iv + pe_iv) / 2.0)
    elif ce_iv is not None:
        atm_iv = float(ce_iv)
    elif pe_iv is not None:
        atm_iv = float(pe_iv)

    hist = state.iv_history_expiry if expiry_type == "EXPIRY_DAY" else state.iv_history_non_expiry
    iv_percentile = None
    if atm_iv is not None and len(hist) > 0:
        arr = np.asarray(list(hist), dtype=float)
        arr = arr[np.isfinite(arr)]
        if arr.size > 0:
            iv_percentile = float(100.0 * (np.sum(arr <= float(atm_iv)) / arr.size))
    if atm_iv is not None and np.isfinite(atm_iv):
        hist.append(float(atm_iv))

    iv_regime = None
    if iv_percentile is not None:
        if iv_percentile < 40.0:
            iv_regime = "CHEAP"
        elif iv_percentile <= 75.0:
            iv_regime = "NEUTRAL"
        else:
            iv_regime = "EXPENSIVE"
    mss8 = {
        "iv_skew": _nullable_float(iv_skew),
        "iv_skew_dir": iv_skew_dir,
        "iv_percentile": _nullable_float(iv_percentile),
        "iv_regime": iv_regime,
        "iv_expiry_type": expiry_type,
    }
    mss_ladder = _ladder_aggregates(
        strikes,
        atm_strike=atm_strike,
        total_ce_oi=total_ce_oi,
        total_pe_oi=total_pe_oi,
        total_ce_volume=total_ce_volume,
        total_pe_volume=total_pe_volume,
    )

    prev_day_key = None
    current_day_key = str(trade_date.date())
    prior_day_values = sorted({str(x.get("trade_date")) for x in state.chain_history if str(x.get("trade_date")) < current_day_key})
    if prior_day_values:
        prev_day_key = prior_day_values[-1]

    prev_day_pcr = None
    prev_day_max_pain = None
    if isinstance(prev_session_chain_baseline, dict):
        prev_day_pcr = _nullable_float(prev_session_chain_baseline.get("pcr"))
        prev_day_max_pain = _nullable_int(prev_session_chain_baseline.get("max_pain"))
    if prev_day_key is not None and (prev_day_pcr is None or prev_day_max_pain is None):
        prev_items = [x for x in state.chain_history if str(x.get("trade_date")) == prev_day_key]
        if prev_items:
            last_prev = prev_items[-1]
            if prev_day_pcr is None:
                prev_day_pcr = _nullable_float(last_prev.get("pcr"))
            if prev_day_max_pain is None:
                prev_day_max_pain = _nullable_int(last_prev.get("max_pain"))
    mss9 = {
        "prev_day_high": _nullable_float(prepared_window.session_levels.get("prev_day_high")),
        "prev_day_low": _nullable_float(prepared_window.session_levels.get("prev_day_low")),
        "prev_day_close": _nullable_float(prepared_window.session_levels.get("prev_day_close")),
        "week_high": _nullable_float(prepared_window.session_levels.get("week_high")),
        "week_low": _nullable_float(prepared_window.session_levels.get("week_low")),
        "overnight_gap": _nullable_float(prepared_window.session_levels.get("overnight_gap")),
        "prev_day_pcr": prev_day_pcr,
        "prev_day_max_pain": prev_day_max_pain,
    }

    state.chain_history.append(
        {
            "timestamp": ts,
            "trade_date": str(trade_date.date()),
            "pcr": _nullable_float(pcr),
            "max_pain": max_pain,
            "atm_strike": _nullable_float(atm_strike),
            "atm_ce_oi": _nullable_float(ce_oi),
            "atm_pe_oi": _nullable_float(pe_oi),
            "atm_ce_volume": _nullable_float(ce_vol),
            "atm_pe_volume": _nullable_float(pe_vol),
        }
    )
    state.option_price_history.append(
        {
            "timestamp": ts,
            "trade_date": str(trade_date.date()),
            "atm_strike": _nullable_float(atm_strike),
            "atm_ce_close": _nullable_float(ce_ltp),
            "atm_pe_close": _nullable_float(pe_ltp),
            "atm_ce_oi": _nullable_float(ce_oi),
            "atm_pe_oi": _nullable_float(pe_oi),
        }
    )

    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "instrument": str(instrument or ""),
        "trade_date": str(trade_date.date()),
        "timestamp": isoformat_ist(ts.to_pydatetime(), naive_mode=TimestampSourceMode.MARKET_IST),
        "session_context": mss1,
        "futures_bar": mss2,
        "futures_derived": mss3,
        "mtf_derived": mss_mtf,
        "opening_range": mss4,
        "vix_context": mss5,
        "strikes": strikes,
        "chain_aggregates": mss6,
        "ladder_aggregates": mss_ladder,
        "atm_options": mss7,
        "iv_derived": mss8,
        "session_levels": mss9,
    }


class LiveMarketSnapshotBuilder:
    def __init__(
        self,
        *,
        instrument: str,
        market_api_base: str = "http://127.0.0.1:8004",
        dashboard_api_base: str = "http://127.0.0.1:8002",
        timeout_seconds: float = 5.0,
        risk_free_rate_default: float = 0.065,
        enable_kite_backfill: bool = True,
        kite_history_days: int = 12,
    ):
        self.instrument = str(instrument or "").strip().upper()
        if not self.instrument:
            raise ValueError("instrument is required")
        self.market_api_base = market_api_base.rstrip("/")
        self.dashboard_api_base = dashboard_api_base.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.state = MarketSnapshotState()
        self.risk_free_rate_default = float(risk_free_rate_default)
        disable_backfill_env = _truthy(os.getenv("ML_PIPELINE_DISABLE_KITE_BACKFILL", "0"))
        self.enable_kite_backfill = bool(enable_kite_backfill and not disable_backfill_env)
        self.kite_history_days = max(2, int(kite_history_days))
        self._kite_client: Any = None
        self._kite_client_unavailable = False
        self._kite_instrument_token: Optional[int] = None
        self._kite_history_cache: pd.DataFrame = pd.DataFrame()
        self._kite_history_cache_end_date: Optional[datetime.date] = None
        # VIX for live snapshots is sourced strictly from market API stream/tick endpoints.
        self.vix_daily = pd.DataFrame()

    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = requests.get(
            url,
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def fetch_ohlc(self, limit: int = 1800) -> pd.DataFrame:
        params = {"timeframe": "1m", "limit": int(limit), "order": "asc"}
        endpoints = [
            f"{self.market_api_base}/api/v1/market/ohlc/{self.instrument}",
            f"{self.market_api_base}/api/v1/ohlc/{self.instrument}",
            f"{self.dashboard_api_base}/api/market-data/ohlc/{self.instrument}",
        ]
        for url in endpoints:
            try:
                payload = self._get_json(url, params=params)
                if isinstance(payload, list):
                    frame = _normalize_ohlc_frame(pd.DataFrame(payload))
                    if len(frame):
                        return frame
            except Exception:
                continue
        return pd.DataFrame()

    def _get_kite_client(self) -> Optional[Any]:
        if self._kite_client is not None:
            return self._kite_client
        if self._kite_client_unavailable:
            return None
        api_key, access_token = _load_kite_credentials()
        if not api_key or not access_token:
            self._kite_client_unavailable = True
            return None
        try:
            self._kite_client = _build_kite_client(api_key=api_key, access_token=access_token)
            return self._kite_client
        except Exception:
            self._kite_client_unavailable = True
            return None

    def _resolve_kite_instrument_token(self, kite: Any) -> Optional[int]:
        if self._kite_instrument_token is not None:
            return self._kite_instrument_token
        try:
            rows = kite.instruments("NFO")
        except Exception:
            return None
        if not isinstance(rows, list) or len(rows) == 0:
            return None

        symbol = self.instrument.upper()
        exact = next(
            (
                row
                for row in rows
                if str(row.get("tradingsymbol") or "").upper() == symbol and str(row.get("instrument_token") or "").isdigit()
            ),
            None,
        )
        if exact is not None:
            self._kite_instrument_token = int(exact.get("instrument_token"))
            return self._kite_instrument_token

        if symbol.endswith("FUT"):
            base = _extract_underlying_symbol(symbol)
            fut_rows = [
                row
                for row in rows
                if str(row.get("instrument_type") or "").upper() == "FUT"
                and str(row.get("name") or "").upper() == base
                and str(row.get("instrument_token") or "").isdigit()
            ]
            if fut_rows:
                today = pd.Timestamp.now(tz=IST).date()
                parsed: List[tuple[datetime.date, Dict[str, Any]]] = []
                for row in fut_rows:
                    exp = pd.to_datetime(row.get("expiry"), errors="coerce")
                    if pd.isna(exp):
                        continue
                    parsed.append((pd.Timestamp(exp).date(), row))
                if parsed:
                    future = [item for item in parsed if item[0] >= today]
                    chosen = min(future, key=lambda x: x[0]) if future else max(parsed, key=lambda x: x[0])
                    self._kite_instrument_token = int(chosen[1].get("instrument_token"))
                    return self._kite_instrument_token
        return None

    def _fetch_kite_previous_days_ohlc(self, latest_ts: pd.Timestamp) -> pd.DataFrame:
        if not self.enable_kite_backfill:
            return pd.DataFrame()

        end_date = (pd.Timestamp(latest_ts).date() - timedelta(days=1))
        if self._kite_history_cache_end_date == end_date and len(self._kite_history_cache):
            return self._kite_history_cache.copy()

        kite = self._get_kite_client()
        if kite is None:
            return pd.DataFrame()
        token = self._resolve_kite_instrument_token(kite)
        if token is None:
            return pd.DataFrame()

        lookback_days = max(14, int(self.kite_history_days) * 2)
        start_date = end_date - timedelta(days=lookback_days)
        if start_date >= end_date:
            return pd.DataFrame()

        use_continuous = self.instrument.upper().endswith("FUT")
        try:
            rows = kite.historical_data(
                instrument_token=token,
                from_date=start_date,
                to_date=end_date,
                interval="minute",
                continuous=use_continuous,
                oi=True,
            )
        except Exception as exc:
            if "invalid interval for continuous data" in str(exc).lower():
                try:
                    rows = kite.historical_data(
                        instrument_token=token,
                        from_date=start_date,
                        to_date=end_date,
                        interval="minute",
                        continuous=False,
                        oi=True,
                    )
                except Exception:
                    return pd.DataFrame()
            else:
                return pd.DataFrame()

        if not isinstance(rows, list) or len(rows) == 0:
            return pd.DataFrame()

        frame = pd.DataFrame(rows)
        if "date" not in frame.columns:
            return pd.DataFrame()
        frame = frame.rename(columns={"date": "timestamp"})
        if "oi" not in frame.columns and "open_interest" in frame.columns:
            frame["oi"] = frame["open_interest"]
        for col in ("open", "high", "low", "close", "volume", "oi"):
            if col not in frame.columns:
                frame[col] = np.nan
        out = _normalize_ohlc_frame(frame.loc[:, ["timestamp", "open", "high", "low", "close", "volume", "oi"]])
        if len(out):
            self._kite_history_cache = out.copy()
            self._kite_history_cache_end_date = end_date
        return out

    def _augment_ohlc_with_kite_history(self, ohlc: pd.DataFrame) -> pd.DataFrame:
        if ohlc is None or len(ohlc) == 0:
            return pd.DataFrame()
        latest_ts = pd.Timestamp(ohlc["timestamp"].iloc[-1])
        prev_days = self._fetch_kite_previous_days_ohlc(latest_ts=latest_ts)
        if prev_days is None or len(prev_days) == 0:
            return ohlc
        return _merge_ohlc_history(primary=ohlc, supplemental=prev_days)

    def fetch_options_chain(self) -> Dict[str, Any]:
        try:
            payload = self._get_json(f"{self.market_api_base}/api/v1/options/chain/{self.instrument}")
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        payload = self._get_json(f"{self.dashboard_api_base}/api/market-data/options/{self.instrument}")
        if isinstance(payload, dict):
            return payload
        return {}

    def fetch_live_vix(self) -> Optional[float]:
        candidates = [
            "INDIA VIX",
            "INDIAVIX",
            "NIFTYVIX",
            "VIX",
        ]
        for symbol in candidates:
            try:
                payload = self._get_json(f"{self.market_api_base}/api/v1/market/tick/{symbol}")
                if isinstance(payload, dict):
                    price = _nullable_float(payload.get("last_price"))
                    if price is not None and price > 0:
                        return price
            except Exception:
                continue
        return None

    def build_snapshot(
        self,
        ohlc_limit: int = 1800,
        prev_session_chain_baseline: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ohlc = self.fetch_ohlc(limit=int(ohlc_limit))
        if len(ohlc) == 0:
            raise RuntimeError(f"no OHLC bars available for {self.instrument}")
        ohlc = self._augment_ohlc_with_kite_history(ohlc=ohlc)
        chain = self.fetch_options_chain()
        vix_live = self.fetch_live_vix()
        return build_market_snapshot(
            instrument=self.instrument,
            ohlc=ohlc,
            chain=chain,
            state=self.state,
            vix_daily=self.vix_daily,
            vix_live_current=vix_live,
            prev_session_chain_baseline=prev_session_chain_baseline,
            risk_free_rate_default=self.risk_free_rate_default,
        )


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build live MarketSnapshot (MSS.1-MSS.9) from market APIs")
    parser.add_argument("--instrument", required=True, help="Instrument symbol, e.g. BANKNIFTY26MARFUT")
    parser.add_argument("--market-api-base", default="http://127.0.0.1:8004")
    parser.add_argument("--dashboard-api-base", default="http://127.0.0.1:8002")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--ohlc-limit", type=int, default=1800, help="1m bars to fetch for derived/session levels")
    parser.add_argument("--out-jsonl", default=None, help="Optional output JSONL path")
    parser.add_argument("--disable-kite-backfill", action="store_true", help="Disable automatic Zerodha history backfill")
    parser.add_argument("--kite-history-days", type=int, default=12, help="Trading-history lookback target used for Kite backfill")
    args = parser.parse_args(list(argv) if argv is not None else None)

    builder = LiveMarketSnapshotBuilder(
        instrument=str(args.instrument),
        market_api_base=str(args.market_api_base),
        dashboard_api_base=str(args.dashboard_api_base),
        timeout_seconds=float(args.timeout_seconds),
        enable_kite_backfill=(not bool(args.disable_kite_backfill)),
        kite_history_days=int(args.kite_history_days),
    )

    snap = builder.build_snapshot(ohlc_limit=int(args.ohlc_limit))
    line = json.dumps(snap, ensure_ascii=False)
    print(line)

    if args.out_jsonl:
        out_path = Path(args.out_jsonl)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
