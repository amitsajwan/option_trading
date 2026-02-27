from typing import Dict, Optional

import numpy as np
import pandas as pd


def safe_float(value: object) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0)).abs()
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical * df["volume"].fillna(0.0)
    return pv.cumsum() / df["volume"].fillna(0.0).cumsum().replace(0.0, np.nan)


def extract_option_slice_from_chain(chain: Dict[str, object], fut_price: float) -> Dict[str, float]:
    strikes = chain.get("strikes") if isinstance(chain, dict) else None
    if not isinstance(strikes, list) or len(strikes) == 0:
        return {}
    rows = [x for x in strikes if isinstance(x, dict) and x.get("strike") is not None]
    if not rows:
        return {}
    rows = sorted(rows, key=lambda x: float(x["strike"]))
    strike_values = [float(x["strike"]) for x in rows]
    step = 100.0
    if len(strike_values) > 1:
        diffs = [b - a for a, b in zip(strike_values[:-1], strike_values[1:]) if (b - a) > 0]
        if diffs:
            step = float(pd.Series(diffs).mode().iloc[0])
    index = {float(x["strike"]): x for x in rows}
    atm_rounded = round(float(fut_price) / float(step)) * float(step) if np.isfinite(safe_float(fut_price)) else float("nan")
    if np.isfinite(atm_rounded) and (float(atm_rounded) in index):
        atm = float(atm_rounded)
    else:
        atm = min(strike_values, key=lambda s: abs(s - fut_price))

    def fill_strike(base: float, rel_name: str) -> Dict[str, float]:
        node = index.get(base)
        if not node:
            return {}
        ce_ltp = safe_float(node.get("ce_ltp"))
        pe_ltp = safe_float(node.get("pe_ltp"))
        ce_oi = safe_float(node.get("ce_oi"))
        pe_oi = safe_float(node.get("pe_oi"))
        ce_vol = safe_float(node.get("ce_volume"))
        pe_vol = safe_float(node.get("pe_volume"))
        out = {
            f"strike_{rel_name}": float(base),
            f"opt_{rel_name}_ce_close": ce_ltp,
            f"opt_{rel_name}_ce_oi": ce_oi,
            f"opt_{rel_name}_ce_volume": ce_vol,
            f"opt_{rel_name}_pe_close": pe_ltp,
            f"opt_{rel_name}_pe_oi": pe_oi,
            f"opt_{rel_name}_pe_volume": pe_vol,
        }
        for side in ("ce", "pe"):
            close_key = f"opt_{rel_name}_{side}_close"
            for fld in ("open", "high", "low"):
                out[f"opt_{rel_name}_{side}_{fld}"] = out[close_key]
        return out

    ce_oi_total = float(np.nansum([safe_float(x.get("ce_oi")) for x in rows]))
    pe_oi_total = float(np.nansum([safe_float(x.get("pe_oi")) for x in rows]))
    pcr_from_totals = float(pe_oi_total / ce_oi_total) if ce_oi_total > 0 else float("nan")
    pcr_live = safe_float(chain.get("pcr"))
    result: Dict[str, float] = {
        "strike_step": step,
        "atm_strike": float(atm),
        "ce_oi_total": ce_oi_total,
        "pe_oi_total": pe_oi_total,
        "ce_volume_total": float(np.nansum([safe_float(x.get("ce_volume")) for x in rows])),
        "pe_volume_total": float(np.nansum([safe_float(x.get("pe_volume")) for x in rows])),
        "pcr_oi": pcr_from_totals if np.isfinite(pcr_from_totals) else pcr_live,
        "options_rows": float(len(rows)),
    }
    result.update(fill_strike(atm - step, "m1"))
    result.update(fill_strike(atm, "0"))
    result.update(fill_strike(atm + step, "p1"))
    return result


def chain_from_options_minute(options_minute: pd.DataFrame) -> Dict[str, object]:
    if options_minute is None or len(options_minute) == 0:
        return {"strikes": [], "expiry": "", "pcr": float("nan")}
    work = options_minute.copy()
    work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
    work = work.dropna(subset=["strike"])
    if len(work) == 0:
        return {"strikes": [], "expiry": "", "pcr": float("nan")}
    strikes: list[dict] = []
    ce_total = 0.0
    pe_total = 0.0
    for strike, grp in work.groupby("strike", sort=True):
        ce = grp[grp["option_type"] == "CE"]
        pe = grp[grp["option_type"] == "PE"]
        ce_ltp = safe_float(ce["close"].iloc[-1]) if len(ce) else float("nan")
        pe_ltp = safe_float(pe["close"].iloc[-1]) if len(pe) else float("nan")
        ce_oi = safe_float(ce["oi"].iloc[-1]) if len(ce) else float("nan")
        pe_oi = safe_float(pe["oi"].iloc[-1]) if len(pe) else float("nan")
        ce_vol = safe_float(ce["volume"].iloc[-1]) if len(ce) else float("nan")
        pe_vol = safe_float(pe["volume"].iloc[-1]) if len(pe) else float("nan")
        if np.isfinite(ce_oi):
            ce_total += float(ce_oi)
        if np.isfinite(pe_oi):
            pe_total += float(pe_oi)
        strikes.append(
            {
                "strike": float(strike),
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_volume": ce_vol,
                "pe_volume": pe_vol,
            }
        )
    expiry = ""
    if "expiry_code" in work.columns:
        non_null = work["expiry_code"].dropna()
        if len(non_null):
            expiry = str(non_null.iloc[0])
    pcr = float(pe_total / ce_total) if ce_total > 0 else float("nan")
    return {"strikes": strikes, "expiry": expiry, "pcr": pcr}


def build_canonical_event_from_ohlc_and_chain(
    *,
    ohlc: pd.DataFrame,
    chain: Dict[str, object],
    vix_snapshot: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    latest = ohlc.iloc[-1]
    row: Dict[str, object] = {
        "timestamp": pd.Timestamp(latest["timestamp"]).isoformat(),
        "trade_date": str(pd.Timestamp(latest["timestamp"]).date()),
        "fut_open": safe_float(latest.get("open")),
        "fut_high": safe_float(latest.get("high")),
        "fut_low": safe_float(latest.get("low")),
        "fut_close": safe_float(latest.get("close")),
        "fut_volume": safe_float(latest.get("volume")),
        "fut_oi": safe_float(latest.get("oi")),
    }

    close = ohlc["close"].astype(float)
    row["ret_1m"] = safe_float(close.pct_change(1, fill_method=None).iloc[-1])
    row["ret_3m"] = safe_float(close.pct_change(3, fill_method=None).iloc[-1])
    row["ret_5m"] = safe_float(close.pct_change(5, fill_method=None).iloc[-1])
    row["ema_9"] = safe_float(close.ewm(span=9, adjust=False).mean().iloc[-1])
    row["ema_21"] = safe_float(close.ewm(span=21, adjust=False).mean().iloc[-1])
    row["ema_50"] = safe_float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    row["ema_9_21_spread"] = safe_float(row["ema_9"] - row["ema_21"]) if np.isfinite(safe_float(row["ema_9"])) and np.isfinite(safe_float(row["ema_21"])) else float("nan")
    row["ema_9_slope"] = safe_float(close.ewm(span=9, adjust=False).mean().diff().iloc[-1])
    row["ema_21_slope"] = safe_float(close.ewm(span=21, adjust=False).mean().diff().iloc[-1])
    row["ema_50_slope"] = safe_float(close.ewm(span=50, adjust=False).mean().diff().iloc[-1])
    row["rsi_14"] = safe_float(rsi(close, 14).iloc[-1])
    atr_series = atr(ohlc, 14)
    row["atr_14"] = safe_float(atr_series.iloc[-1])
    row["atr_ratio"] = safe_float(row["atr_14"] / row["fut_close"]) if np.isfinite(row["fut_close"]) else float("nan")
    row["atr_percentile"] = safe_float(atr_series.rank(pct=True).iloc[-1])
    vwap_series = vwap(ohlc)
    row["fut_vwap"] = safe_float(vwap_series.iloc[-1])
    row["vwap_distance"] = safe_float((row["fut_close"] - row["fut_vwap"]) / row["fut_vwap"]) if np.isfinite(row["fut_vwap"]) else float("nan")
    row["distance_from_day_high"] = safe_float((row["fut_close"] - float(ohlc["high"].cummax().iloc[-1])) / float(ohlc["high"].cummax().iloc[-1]))
    row["distance_from_day_low"] = safe_float((row["fut_close"] - float(ohlc["low"].cummin().iloc[-1])) / float(ohlc["low"].cummin().iloc[-1]))

    ts = pd.Timestamp(latest["timestamp"])
    row["minute_of_day"] = int(ts.hour * 60 + ts.minute)
    row["day_of_week"] = int(ts.dayofweek)
    row["minute_index"] = int(len(ohlc) - 1)

    or_window = ohlc.head(min(15, len(ohlc)))
    if len(or_window) > 0:
        or_high = float(or_window["high"].max())
        or_low = float(or_window["low"].min())
        row["opening_range_high"] = or_high
        row["opening_range_low"] = or_low
        ready = int(row["minute_index"]) >= 15
        row["opening_range_ready"] = int(ready)
        row["opening_range_breakout_up"] = int(ready and (row["fut_close"] > or_high))
        row["opening_range_breakout_down"] = int(ready and (row["fut_close"] < or_low))

    row["spot_open"] = float("nan")
    row["spot_high"] = float("nan")
    row["spot_low"] = float("nan")
    row["spot_close"] = float("nan")
    row["basis"] = float("nan")
    row["basis_change_1m"] = float("nan")

    row["expiry_code"] = str(chain.get("expiry", "")).upper().replace("-", "")
    row.update(extract_option_slice_from_chain(chain, fut_price=float(row["fut_close"])))
    row["ce_pe_oi_diff"] = safe_float(row.get("ce_oi_total")) - safe_float(row.get("pe_oi_total"))
    row["ce_pe_volume_diff"] = safe_float(row.get("ce_volume_total")) - safe_float(row.get("pe_volume_total"))

    row["vix_prev_close"] = float("nan")
    row["vix_prev_close_change_1d"] = float("nan")
    row["vix_prev_close_zscore_20d"] = float("nan")
    row["is_high_vix_day"] = 0.0
    if isinstance(vix_snapshot, dict):
        for key in ("vix_prev_close", "vix_prev_close_change_1d", "vix_prev_close_zscore_20d", "is_high_vix_day"):
            if key in vix_snapshot:
                row[key] = safe_float(vix_snapshot.get(key))

    row["atm_call_return_1m"] = float("nan")
    row["atm_put_return_1m"] = float("nan")
    row["atm_oi_change_1m"] = float("nan")
    return row


def build_vix_snapshot_for_trade_date(vix_daily: pd.DataFrame, trade_date: object) -> Dict[str, float]:
    snapshot = {
        "vix_prev_close": float("nan"),
        "vix_prev_close_change_1d": float("nan"),
        "vix_prev_close_zscore_20d": float("nan"),
        "is_high_vix_day": 0.0,
    }
    if vix_daily is None or len(vix_daily) == 0:
        return snapshot
    td = pd.to_datetime(trade_date, errors="coerce")
    if pd.isna(td):
        return snapshot
    vd = vix_daily.copy()
    vd["trade_date"] = pd.to_datetime(vd["trade_date"], errors="coerce")
    vd["vix_close"] = pd.to_numeric(vd["vix_close"], errors="coerce")
    vd = vd.dropna(subset=["trade_date", "vix_close"]).sort_values("trade_date").reset_index(drop=True)
    hist = vd[vd["trade_date"] < td.normalize()].copy()
    if len(hist) == 0:
        return snapshot
    prev_close = float(hist.iloc[-1]["vix_close"])
    snapshot["vix_prev_close"] = prev_close
    if len(hist) >= 2:
        prev2 = float(hist.iloc[-2]["vix_close"])
        if prev2 != 0.0:
            snapshot["vix_prev_close_change_1d"] = float((prev_close - prev2) / prev2)
    tail20 = hist["vix_close"].tail(20)
    if len(tail20) >= 5:
        mu = float(tail20.mean())
        sigma = float(tail20.std(ddof=0))
        if np.isfinite(sigma) and sigma > 0:
            snapshot["vix_prev_close_zscore_20d"] = float((prev_close - mu) / sigma)
    snapshot["is_high_vix_day"] = 1.0 if np.isfinite(prev_close) and prev_close >= 20.0 else 0.0
    return snapshot


def apply_option_change_features(
    row: Dict[str, object],
    *,
    prev_trade_date: Optional[str],
    prev_opt0_ce_close: Optional[float],
    prev_opt0_pe_close: Optional[float],
    prev_opt0_total_oi: Optional[float],
) -> tuple[Optional[str], Optional[float], Optional[float], Optional[float]]:
    trade_date = str(row.get("trade_date", "")).strip() or None
    ce_close = safe_float(row.get("opt_0_ce_close"))
    pe_close = safe_float(row.get("opt_0_pe_close"))
    ce_oi = safe_float(row.get("opt_0_ce_oi"))
    pe_oi = safe_float(row.get("opt_0_pe_oi"))
    curr_total_oi = ce_oi + pe_oi if np.isfinite(ce_oi) and np.isfinite(pe_oi) else float("nan")

    same_day = bool(trade_date and prev_trade_date and trade_date == prev_trade_date)
    call_ret = float("nan")
    put_ret = float("nan")
    oi_change = float("nan")
    if same_day:
        if np.isfinite(ce_close) and np.isfinite(safe_float(prev_opt0_ce_close)) and float(prev_opt0_ce_close) != 0.0:
            call_ret = float((ce_close - float(prev_opt0_ce_close)) / float(prev_opt0_ce_close))
        if np.isfinite(pe_close) and np.isfinite(safe_float(prev_opt0_pe_close)) and float(prev_opt0_pe_close) != 0.0:
            put_ret = float((pe_close - float(prev_opt0_pe_close)) / float(prev_opt0_pe_close))
        if np.isfinite(curr_total_oi) and np.isfinite(safe_float(prev_opt0_total_oi)):
            oi_change = float(curr_total_oi - float(prev_opt0_total_oi))

    row["atm_call_return_1m"] = call_ret
    row["atm_put_return_1m"] = put_ret
    row["atm_oi_change_1m"] = oi_change

    next_ce = float(ce_close) if np.isfinite(ce_close) else None
    next_pe = float(pe_close) if np.isfinite(pe_close) else None
    next_oi = float(curr_total_oi) if np.isfinite(curr_total_oi) else None
    return trade_date, next_ce, next_pe, next_oi
