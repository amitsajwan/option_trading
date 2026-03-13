from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Optional

import numpy as np
import pandas as pd

from snapshot_app.market_snapshot import (
    LiveMarketSnapshotBuilder,
    _extract_underlying_symbol,
    _normalize_ohlc_frame,
)


_CONTRACT_CACHE: Dict[str, Dict[str, Any]] = {}
_PANEL_MAX_ROWS = 25000
_PANEL_SOURCE_COLUMNS = [
    "timestamp",
    "trade_date",
    "fut_open",
    "fut_high",
    "fut_low",
    "fut_close",
    "fut_volume",
    "fut_oi",
    "spot_open",
    "spot_high",
    "spot_low",
    "spot_close",
    "expiry_code",
    "strike_step",
    "atm_strike",
    "ce_oi_total",
    "pe_oi_total",
    "ce_volume_total",
    "pe_volume_total",
    "pcr_oi",
    "options_rows",
    "strike_m1",
    "strike_0",
    "strike_p1",
    "opt_m1_ce_open",
    "opt_m1_ce_high",
    "opt_m1_ce_low",
    "opt_m1_ce_close",
    "opt_m1_ce_oi",
    "opt_m1_ce_volume",
    "opt_m1_pe_open",
    "opt_m1_pe_high",
    "opt_m1_pe_low",
    "opt_m1_pe_close",
    "opt_m1_pe_oi",
    "opt_m1_pe_volume",
    "opt_0_ce_open",
    "opt_0_ce_high",
    "opt_0_ce_low",
    "opt_0_ce_close",
    "opt_0_ce_oi",
    "opt_0_ce_volume",
    "opt_0_pe_open",
    "opt_0_pe_high",
    "opt_0_pe_low",
    "opt_0_pe_close",
    "opt_0_pe_oi",
    "opt_0_pe_volume",
    "opt_p1_ce_open",
    "opt_p1_ce_high",
    "opt_p1_ce_low",
    "opt_p1_ce_close",
    "opt_p1_ce_oi",
    "opt_p1_ce_volume",
    "opt_p1_pe_open",
    "opt_p1_pe_high",
    "opt_p1_pe_low",
    "opt_p1_pe_close",
    "opt_p1_pe_oi",
    "opt_p1_pe_volume",
]


def _ensure_ml_pipeline_src_on_path() -> None:
    src = Path(__file__).resolve().parents[1] / "ml_pipeline" / "src"
    src_str = str(src)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def _contract_context(contract_dir: Optional[Path] = None) -> Dict[str, Any]:
    key = str(Path(contract_dir).resolve()) if contract_dir is not None else "<default>"
    cached = _CONTRACT_CACHE.get(key)
    if cached is not None:
        return cached

    _ensure_ml_pipeline_src_on_path()

    import ml_pipeline.feature.engineering as feature_engineering
    from ml_pipeline.snapshot_ml_flat_contract import (
        load_contract_schema,
        load_legacy_mapping,
        validate_snapshot_ml_flat_rows,
    )

    schema = load_contract_schema(contract_dir=contract_dir)
    mapping = load_legacy_mapping(contract_dir=contract_dir)
    rename_map: Dict[str, str] = {}
    for row in mapping.to_dict(orient="records"):
        legacy = str(row.get("legacy_name") or "").strip()
        new = str(row.get("new_name") or "").strip()
        removed = str(row.get("is_removed") or "").strip().lower() == "true"
        if legacy and new and (not removed):
            rename_map[legacy] = new

    ctx = {
        "feature_engineering": feature_engineering,
        "validate_snapshot_ml_flat_rows": validate_snapshot_ml_flat_rows,
        "schema_name": str(schema.get("schema_name") or "SnapshotMLFlat"),
        "schema_version": str(schema.get("schema_version") or "1.0.0"),
        "field_types": dict(schema.get("field_types") or {}),
        "required_columns": [str(x) for x in list(schema.get("required_columns") or [])],
        "rename_map": rename_map,
    }
    _CONTRACT_CACHE[key] = ctx
    return ctx


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except Exception:
        return False


def _safe_rsi_last(close: pd.Series, period: int = 14) -> float:
    values = pd.to_numeric(close, errors="coerce").dropna()
    p = max(2, int(period))
    if len(values) < (p + 1):
        return float("nan")
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
        return float(100.0 - (100.0 / (1.0 + rs)))
    return float("nan")


def _nullable_float(value: Any) -> Optional[float]:
    out = _safe_float(value)
    return float(out) if np.isfinite(out) else None


def _nullable_int(value: Any) -> Optional[int]:
    out = _safe_float(value)
    return int(round(float(out))) if np.isfinite(out) else None


def _session_phase_from_ts(ts: pd.Timestamp) -> str:
    minute = int(ts.hour * 60 + ts.minute)
    if 555 <= minute < 585:
        return "DISCOVERY"
    if 585 <= minute < 870:
        return "ACTIVE"
    if 870 <= minute <= 930:
        return "PRE_CLOSE"
    return "CLOSED"


def _minutes_since_open(ts: pd.Timestamp) -> Optional[int]:
    session_open = ts.normalize() + pd.Timedelta(hours=9, minutes=15)
    delta = int((ts - session_open) / pd.Timedelta(minutes=1))
    return delta if delta >= 0 else None


def _series_last_pct_change(series: pd.Series, periods: int) -> Optional[float]:
    values = pd.to_numeric(series, errors="coerce")
    if len(values) <= int(periods):
        return None
    out = values.pct_change(periods, fill_method=None).iloc[-1]
    return _nullable_float(out)


def _series_last_diff(series: pd.Series, periods: int) -> Optional[float]:
    values = pd.to_numeric(series, errors="coerce")
    if len(values) <= int(periods):
        return None
    out = values.diff(periods).iloc[-1]
    return _nullable_float(out)


def _build_runtime_compat_payload(
    *,
    feature_row: Dict[str, Any],
    panel: pd.DataFrame,
    chain: Dict[str, Any],
    vix_live_current: Optional[float],
    ts: pd.Timestamp,
) -> Dict[str, Any]:
    work = panel.sort_values("timestamp").copy()
    closes = pd.to_numeric(work.get("fut_close"), errors="coerce")
    rets = closes.pct_change(fill_method=None)
    realized_vol_30m = _nullable_float(rets.rolling(30, min_periods=10).std().iloc[-1]) if len(rets) else None
    if len(work) >= 40:
        same_day = work[work["trade_date"].astype(str) == str(ts.date())].copy()
        same_day_rets = pd.to_numeric(same_day.get("fut_close"), errors="coerce").pct_change(fill_method=None)
        baseline = same_day_rets.rolling(30, min_periods=10).std().expanding(min_periods=20).mean().iloc[-1]
        baseline_value = _nullable_float(baseline)
    else:
        baseline_value = None
    vol_ratio = None
    if realized_vol_30m is not None and baseline_value is not None and baseline_value > 0.0:
        vol_ratio = float(realized_vol_30m / baseline_value)

    strikes = [dict(row) for row in list(chain.get("strikes") or []) if isinstance(row, dict)]
    atm_strike = _nullable_int(feature_row.get("opt_flow_atm_strike"))
    atm_row = None
    if atm_strike is not None:
        for row in strikes:
            strike = _nullable_int(row.get("strike"))
            if strike == atm_strike:
                atm_row = row
                break

    vix_prev_close = _nullable_float(feature_row.get("vix_prev_close"))
    vix_current = _nullable_float(vix_live_current)
    vix_intraday_chg = None
    if vix_current is not None and vix_prev_close is not None and vix_prev_close != 0.0:
        vix_intraday_chg = float(((vix_current - vix_prev_close) / vix_prev_close) * 100.0)
    if vix_current is None and _nullable_float(feature_row.get("ctx_is_high_vix_day")) == 1.0:
        vix_current = 20.0
    if vix_current is not None and vix_current < 14.0:
        vix_regime = "LOW"
    elif vix_current is not None and vix_current <= 20.0:
        vix_regime = "NORMAL"
    elif vix_current is not None:
        vix_regime = "HIGH"
    else:
        vix_regime = ""

    opening_range_high = _nullable_float(feature_row.get("opening_range_high"))
    opening_range_low = _nullable_float(feature_row.get("opening_range_low"))
    fut_close = _nullable_float(feature_row.get("px_fut_close") or feature_row.get("fut_close"))
    price_vs_orh = None
    price_vs_orl = None
    or_width = None
    if fut_close is not None and opening_range_high not in (None, 0.0):
        price_vs_orh = float((fut_close - opening_range_high) / opening_range_high)
    if fut_close is not None and opening_range_low not in (None, 0.0):
        price_vs_orl = float((fut_close - opening_range_low) / opening_range_low)
    if opening_range_high is not None and opening_range_low is not None:
        or_width = float(opening_range_high - opening_range_low)

    max_pain = None
    if strikes:
        try:
            strike_values = pd.to_numeric(pd.Series([row.get("strike") for row in strikes]), errors="coerce")
            ce_oi = pd.to_numeric(pd.Series([row.get("ce_oi") for row in strikes]), errors="coerce").fillna(0.0)
            pe_oi = pd.to_numeric(pd.Series([row.get("pe_oi") for row in strikes]), errors="coerce").fillna(0.0)
            valid = strike_values.notna()
            if bool(valid.any()):
                strike_arr = strike_values[valid].to_numpy(dtype=float)
                ce_arr = ce_oi[valid].to_numpy(dtype=float)
                pe_arr = pe_oi[valid].to_numpy(dtype=float)
                diff = strike_arr[:, None] - strike_arr[None, :]
                ce_pain = (np.maximum(diff, 0.0) * ce_arr[None, :]).sum(axis=1)
                pe_pain = (np.maximum(-diff, 0.0) * pe_arr[None, :]).sum(axis=1)
                max_pain = int(round(float(strike_arr[np.argmin(ce_pain + pe_pain)])))
        except Exception:
            max_pain = None

    ce_oi_top_strike = None
    pe_oi_top_strike = None
    if strikes:
        try:
            ce_oi_top_strike = _nullable_int(max(strikes, key=lambda row: _safe_float(row.get("ce_oi")))["strike"])
            pe_oi_top_strike = _nullable_int(max(strikes, key=lambda row: _safe_float(row.get("pe_oi")))["strike"])
        except Exception:
            ce_oi_top_strike = None
            pe_oi_top_strike = None

    session_phase = _session_phase_from_ts(ts)
    minutes_since_open = _minutes_since_open(ts)
    trade_date = str(ts.date())
    compatibility = {
        "session_context": {
            "snapshot_id": str(feature_row.get("snapshot_id") or ""),
            "timestamp": pd.Timestamp(ts).isoformat(),
            "date": trade_date,
            "minutes_since_open": minutes_since_open,
            "day_of_week": int(ts.dayofweek),
            "days_to_expiry": _nullable_int(feature_row.get("ctx_dte_days")),
            "is_expiry_day": bool(_nullable_float(feature_row.get("ctx_is_expiry_day")) == 1.0),
            "session_phase": session_phase,
        },
        "futures_bar": {
            "fut_open": _nullable_float(feature_row.get("px_fut_open")),
            "fut_high": _nullable_float(feature_row.get("px_fut_high")),
            "fut_low": _nullable_float(feature_row.get("px_fut_low")),
            "fut_close": fut_close,
            "fut_volume": _nullable_float(feature_row.get("fut_flow_volume")),
            "fut_oi": _nullable_float(feature_row.get("fut_flow_oi")),
        },
        "futures_derived": {
            "fut_return_5m": _nullable_float(feature_row.get("ret_5m")),
            "fut_return_15m": _series_last_pct_change(closes, 15),
            "fut_return_30m": _series_last_pct_change(closes, 30),
            "realized_vol_30m": realized_vol_30m,
            "vol_ratio": vol_ratio,
            "fut_volume_ratio": _nullable_float(feature_row.get("fut_flow_rel_volume_20")),
            "fut_oi_change_30m": _series_last_diff(pd.to_numeric(work.get("fut_oi"), errors="coerce"), 30),
            "ema_9": _nullable_float(feature_row.get("ema_9")),
            "ema_21": _nullable_float(feature_row.get("ema_21")),
            "ema_50": _nullable_float(feature_row.get("ema_50")),
            "vwap": _nullable_float(feature_row.get("vwap_fut")),
            "price_vs_vwap": _nullable_float(feature_row.get("vwap_distance")),
        },
        "opening_range": {
            "orh": opening_range_high,
            "orl": opening_range_low,
            "or_width": or_width,
            "price_vs_orh": price_vs_orh,
            "price_vs_orl": price_vs_orl,
            "orh_broken": bool(_nullable_float(feature_row.get("ctx_opening_range_breakout_up")) == 1.0),
            "orl_broken": bool(_nullable_float(feature_row.get("ctx_opening_range_breakout_down")) == 1.0),
        },
        "vix_context": {
            "vix_current": vix_current,
            "vix_prev_close": vix_prev_close,
            "vix_intraday_chg": vix_intraday_chg,
            "vix_regime": vix_regime,
            "vix_spike_flag": bool(vix_intraday_chg is not None and vix_intraday_chg > 15.0),
        },
        "chain_aggregates": {
            "atm_strike": atm_strike,
            "total_ce_oi": _nullable_float(feature_row.get("opt_flow_ce_oi_total")),
            "total_pe_oi": _nullable_float(feature_row.get("opt_flow_pe_oi_total")),
            "pcr": _nullable_float(feature_row.get("opt_flow_pcr_oi")),
            "max_pain": max_pain,
            "ce_oi_top_strike": ce_oi_top_strike,
            "pe_oi_top_strike": pe_oi_top_strike,
        },
        "atm_options": {
            "atm_ce_close": _nullable_float((atm_row or {}).get("ce_ltp") or feature_row.get("opt_0_ce_close")),
            "atm_pe_close": _nullable_float((atm_row or {}).get("pe_ltp") or feature_row.get("opt_0_pe_close")),
            "atm_ce_open": _nullable_float((atm_row or {}).get("ce_open") or feature_row.get("opt_0_ce_open")),
            "atm_ce_high": _nullable_float((atm_row or {}).get("ce_high") or feature_row.get("opt_0_ce_high")),
            "atm_ce_low": _nullable_float((atm_row or {}).get("ce_low") or feature_row.get("opt_0_ce_low")),
            "atm_pe_open": _nullable_float((atm_row or {}).get("pe_open") or feature_row.get("opt_0_pe_open")),
            "atm_pe_high": _nullable_float((atm_row or {}).get("pe_high") or feature_row.get("opt_0_pe_high")),
            "atm_pe_low": _nullable_float((atm_row or {}).get("pe_low") or feature_row.get("opt_0_pe_low")),
            "atm_ce_volume": _nullable_float((atm_row or {}).get("ce_volume") or feature_row.get("opt_0_ce_volume")),
            "atm_pe_volume": _nullable_float((atm_row or {}).get("pe_volume") or feature_row.get("opt_0_pe_volume")),
            "atm_ce_oi": _nullable_float((atm_row or {}).get("ce_oi") or feature_row.get("opt_0_ce_oi")),
            "atm_pe_oi": _nullable_float((atm_row or {}).get("pe_oi") or feature_row.get("opt_0_pe_oi")),
            "atm_ce_iv": _nullable_float((atm_row or {}).get("ce_iv")),
            "atm_pe_iv": _nullable_float((atm_row or {}).get("pe_iv")),
            "atm_ce_vol_ratio": None,
            "atm_pe_vol_ratio": None,
            "atm_ce_oi_change_30m": None,
            "atm_pe_oi_change_30m": None,
        },
        "iv_derived": {
            "iv_skew": _nullable_float(feature_row.get("iv_skew")),
            "iv_percentile": None,
            "iv_regime": "",
            "iv_expiry_type": "",
        },
        "strikes": strikes,
        "session_phase": session_phase,
        "minutes_since_open": minutes_since_open,
        "fut_return_15m": _series_last_pct_change(closes, 15),
        "fut_return_30m": _series_last_pct_change(closes, 30),
        "realized_vol_30m": realized_vol_30m,
        "vol_ratio": vol_ratio,
        "fut_oi_change_30m": _series_last_diff(pd.to_numeric(work.get("fut_oi"), errors="coerce"), 30),
        "vix_current": vix_current,
        "vix_prev_close": vix_prev_close,
        "vix_intraday_chg": vix_intraday_chg,
        "vix_spike_flag": bool(vix_intraday_chg is not None and vix_intraday_chg > 15.0),
        "max_pain": max_pain,
    }
    return compatibility


def _coerce_contract_value(name: str, value: Any, field_types: Dict[str, str]) -> Any:
    if _is_missing(value):
        return None

    expected = str(field_types.get(name) or "").strip().lower()
    if expected == "string":
        if name == "trade_date":
            ts = pd.to_datetime(value, errors="coerce")
            if pd.notna(ts):
                return str(pd.Timestamp(ts).date())
        return str(value)
    if expected == "datetime":
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return str(value)
        return pd.Timestamp(ts).isoformat()
    if expected == "integer":
        return int(round(float(value)))
    if expected == "number":
        return float(value)
    return value


def _parse_expiry_code(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d%b%y", "%Y%m%d"):
        try:
            dt = pd.to_datetime(text.upper() if "%b" in fmt else text, format=fmt, errors="raise")
            return pd.Timestamp(dt).strftime("%d%b%y").upper()
        except Exception:
            continue
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.notna(parsed):
        return pd.Timestamp(parsed).strftime("%d%b%y").upper()
    return text.upper().replace("-", "")


def _extract_option_slice_from_chain(chain: Dict[str, Any], fut_price: float) -> Dict[str, float]:
    strikes = chain.get("strikes") if isinstance(chain, dict) else None
    if not isinstance(strikes, list) or len(strikes) == 0:
        return {}

    rows = [item for item in strikes if isinstance(item, dict) and item.get("strike") is not None]
    if not rows:
        return {}

    rows = sorted(rows, key=lambda item: float(item["strike"]))
    strike_values = [float(item["strike"]) for item in rows]
    step = 100.0
    if len(strike_values) > 1:
        diffs = [b - a for a, b in zip(strike_values[:-1], strike_values[1:]) if (b - a) > 0]
        if diffs:
            step = float(pd.Series(diffs).mode().iloc[0])

    strike_index = {float(item["strike"]): item for item in rows}
    if np.isfinite(_safe_float(fut_price)):
        atm_rounded = round(float(fut_price) / float(step)) * float(step)
    else:
        atm_rounded = float("nan")
    if np.isfinite(atm_rounded) and float(atm_rounded) in strike_index:
        atm = float(atm_rounded)
    else:
        atm = min(strike_values, key=lambda strike: abs(strike - fut_price))

    def _fill_strike(base: float, rel_name: str) -> Dict[str, float]:
        node = strike_index.get(base)
        if not node:
            return {}
        ce_open = _safe_float(node.get("ce_open"))
        ce_high = _safe_float(node.get("ce_high"))
        ce_low = _safe_float(node.get("ce_low"))
        ce_close = _safe_float(node.get("ce_ltp"))
        ce_oi = _safe_float(node.get("ce_oi"))
        ce_volume = _safe_float(node.get("ce_volume"))
        pe_open = _safe_float(node.get("pe_open"))
        pe_high = _safe_float(node.get("pe_high"))
        pe_low = _safe_float(node.get("pe_low"))
        pe_close = _safe_float(node.get("pe_ltp"))
        pe_oi = _safe_float(node.get("pe_oi"))
        pe_volume = _safe_float(node.get("pe_volume"))
        return {
            f"strike_{rel_name}": float(base),
            f"opt_{rel_name}_ce_open": ce_open,
            f"opt_{rel_name}_ce_high": ce_high,
            f"opt_{rel_name}_ce_low": ce_low,
            f"opt_{rel_name}_ce_close": ce_close,
            f"opt_{rel_name}_ce_oi": ce_oi,
            f"opt_{rel_name}_ce_volume": ce_volume,
            f"opt_{rel_name}_pe_open": pe_open,
            f"opt_{rel_name}_pe_high": pe_high,
            f"opt_{rel_name}_pe_low": pe_low,
            f"opt_{rel_name}_pe_close": pe_close,
            f"opt_{rel_name}_pe_oi": pe_oi,
            f"opt_{rel_name}_pe_volume": pe_volume,
        }

    ce_oi_total = float(np.nansum([_safe_float(item.get("ce_oi")) for item in rows]))
    pe_oi_total = float(np.nansum([_safe_float(item.get("pe_oi")) for item in rows]))
    result: Dict[str, float] = {
        "strike_step": float(step),
        "atm_strike": float(atm),
        "ce_oi_total": ce_oi_total,
        "pe_oi_total": pe_oi_total,
        "ce_volume_total": float(np.nansum([_safe_float(item.get("ce_volume")) for item in rows])),
        "pe_volume_total": float(np.nansum([_safe_float(item.get("pe_volume")) for item in rows])),
        "pcr_oi": (float(pe_oi_total / ce_oi_total) if ce_oi_total > 0 else float("nan")),
        "options_rows": float(len(rows)),
    }
    result.update(_fill_strike(atm - step, "m1"))
    result.update(_fill_strike(atm, "0"))
    result.update(_fill_strike(atm + step, "p1"))
    return result


def _spot_symbol_candidates(instrument: str) -> list[str]:
    underlying = _extract_underlying_symbol(instrument)
    compact = str(underlying or "").replace(" ", "").upper()
    candidates = {
        "BANKNIFTY": ["NIFTY BANK", "BANKNIFTY"],
        "NIFTY": ["NIFTY 50", "NIFTY"],
    }.get(compact, [underlying])
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def _build_price_panel(fut_ohlc: pd.DataFrame, spot_ohlc: pd.DataFrame) -> pd.DataFrame:
    fut = _normalize_ohlc_frame(fut_ohlc)
    if len(fut) == 0:
        raise ValueError("cannot build SnapshotMLFlat: empty futures OHLC frame")

    base = fut.loc[:, ["timestamp", "open", "high", "low", "close", "volume", "oi"]].copy()
    base = base.rename(
        columns={
            "open": "fut_open",
            "high": "fut_high",
            "low": "fut_low",
            "close": "fut_close",
            "volume": "fut_volume",
            "oi": "fut_oi",
        }
    )
    base["trade_date"] = base["timestamp"].dt.date.astype(str)

    spot = _normalize_ohlc_frame(spot_ohlc)
    if len(spot) > 0:
        spot_frame = spot.loc[:, ["timestamp", "open", "high", "low", "close"]].copy()
        spot_frame = spot_frame.rename(
            columns={
                "open": "spot_open",
                "high": "spot_high",
                "low": "spot_low",
                "close": "spot_close",
            }
        )
        spot_frame["trade_date"] = spot_frame["timestamp"].dt.date.astype(str)
        merged = pd.merge_asof(
            base.sort_values("timestamp"),
            spot_frame.sort_values("timestamp"),
            on="timestamp",
            by="trade_date",
            direction="backward",
        )
    else:
        merged = base.copy()
        for col in ("spot_open", "spot_high", "spot_low", "spot_close"):
            merged[col] = np.nan

    for col in _PANEL_SOURCE_COLUMNS:
        if col not in merged.columns:
            merged[col] = np.nan
    merged["expiry_code"] = merged["expiry_code"].astype(object)
    merged = merged.loc[:, _PANEL_SOURCE_COLUMNS].copy()
    merged["timestamp"] = pd.to_datetime(merged["timestamp"], errors="coerce")
    merged = merged.dropna(subset=["timestamp"]).sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
    return merged.reset_index(drop=True)


def _state_frame(state: "SnapshotMLFlatState") -> pd.DataFrame:
    if state is None or not state.panel_rows:
        return pd.DataFrame(columns=_PANEL_SOURCE_COLUMNS)
    frame = pd.DataFrame(list(state.panel_rows))
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame = frame.dropna(subset=["timestamp"])
    for col in _PANEL_SOURCE_COLUMNS:
        if col not in frame.columns:
            frame[col] = np.nan
    frame = frame.loc[:, _PANEL_SOURCE_COLUMNS].copy()
    frame = frame.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
    return frame.reset_index(drop=True)


def _merge_panel_history(base_panel: pd.DataFrame, history_panel: pd.DataFrame) -> pd.DataFrame:
    if history_panel is None or len(history_panel) == 0:
        return base_panel.copy()
    base = base_panel.set_index("timestamp")
    hist = history_panel.set_index("timestamp")
    merged = base.combine_first(hist)
    merged = merged.reset_index()
    for col in _PANEL_SOURCE_COLUMNS:
        if col not in merged.columns:
            merged[col] = np.nan
    merged = merged.loc[:, _PANEL_SOURCE_COLUMNS].copy()
    merged["timestamp"] = pd.to_datetime(merged["timestamp"], errors="coerce")
    merged = merged.dropna(subset=["timestamp"]).sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
    return merged.reset_index(drop=True)


def _upsert_panel_row(panel: pd.DataFrame, row: Dict[str, Any]) -> pd.DataFrame:
    if len(panel):
        work = panel.set_index("timestamp")
    else:
        work = pd.DataFrame(columns=_PANEL_SOURCE_COLUMNS).set_index("timestamp")
    ts = pd.Timestamp(row["timestamp"])
    if ts not in work.index:
        work.loc[ts, :] = np.nan
    for key, value in row.items():
        if key == "timestamp":
            continue
        work.loc[ts, key] = value
    work = work.reset_index()
    for col in _PANEL_SOURCE_COLUMNS:
        if col not in work.columns:
            work[col] = np.nan
    work = work.loc[:, _PANEL_SOURCE_COLUMNS].copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work = work.dropna(subset=["timestamp"]).sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
    return work.reset_index(drop=True)


def _snapshot_id_from_ts(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).isoformat()


def _resolve_high_vix_flag(vix_live_current: Optional[float], fallback: Any) -> int:
    live_value = _safe_float(vix_live_current)
    if np.isfinite(live_value):
        return int(live_value >= 20.0)
    fallback_value = _safe_float(fallback)
    if np.isfinite(fallback_value):
        return int(fallback_value >= 0.5)
    return 0


@dataclass
class SnapshotMLFlatState:
    panel_rows: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=_PANEL_MAX_ROWS))


def build_snapshot_ml_flat_from_inputs(
    *,
    instrument: str,
    fut_ohlc: pd.DataFrame,
    spot_ohlc: pd.DataFrame,
    chain: Dict[str, Any],
    state: SnapshotMLFlatState,
    build_run_id: str,
    vix_live_current: Optional[float] = None,
    contract_dir: Optional[Path] = None,
    validate: bool = True,
) -> Dict[str, Any]:
    ctx = _contract_context(contract_dir=contract_dir)
    base_panel = _build_price_panel(fut_ohlc=fut_ohlc, spot_ohlc=spot_ohlc)
    panel = _merge_panel_history(base_panel, _state_frame(state))

    latest = base_panel.iloc[-1]
    ts = pd.Timestamp(latest["timestamp"])
    current_row: Dict[str, Any] = {
        "timestamp": ts,
        "trade_date": str(ts.date()),
        "expiry_code": _parse_expiry_code(chain.get("expiry")),
    }
    current_row.update(_extract_option_slice_from_chain(chain, fut_price=_safe_float(latest.get("fut_close"))))
    panel = _upsert_panel_row(panel, current_row)

    max_rows = state.panel_rows.maxlen or _PANEL_MAX_ROWS
    state.panel_rows = deque(panel.tail(max_rows).to_dict(orient="records"), maxlen=max_rows)

    feature_engineering = ctx["feature_engineering"]
    groups = []
    for _, group in panel.groupby("trade_date", sort=True):
        groups.append(feature_engineering._add_group_features(group))
    if not groups:
        raise ValueError("cannot build SnapshotMLFlat: no grouped panel rows")
    features = pd.concat(groups, ignore_index=True)
    features = feature_engineering._add_dte_features(features)
    features = feature_engineering._add_vix_features(features, vix_source=None)
    features = feature_engineering._add_cross_session_atr_percentile(features)
    features = feature_engineering.attach_regime_features(features)
    features = feature_engineering._add_dealer_proxy_features(features)
    zero_fill_cols = (
        "ema_9_slope",
        "ema_21_slope",
        "ema_50_slope",
        "basis_change_1m",
        "fut_volume_accel_1m",
        "fut_oi_change_1m",
        "fut_oi_change_5m",
        "regime_atr_high",
        "regime_atr_low",
    )
    for col in zero_fill_cols:
        if col in features.columns:
            features[col] = pd.to_numeric(features[col], errors="coerce").fillna(0.0)
    features = features.sort_values("timestamp").reset_index(drop=True)
    if len(features) == 0:
        raise ValueError("cannot build SnapshotMLFlat: feature table is empty")
    feature_row = features.iloc[-1].to_dict()
    same_day_features = features[features["trade_date"].astype(str) == str(ts.date())].copy()
    if _is_missing(feature_row.get("rsi_14")):
        same_day = panel[panel["trade_date"].astype(str) == str(ts.date())].copy()
        feature_row["rsi_14"] = _safe_rsi_last(same_day.get("fut_close", pd.Series(dtype=float)), period=14)
    if _is_missing(feature_row.get("atr_percentile")):
        atr_ratio = pd.to_numeric(same_day_features.get("atr_ratio"), errors="coerce")
        valid_atr_ratio = atr_ratio.dropna()
        if len(same_day_features) >= 21 and len(valid_atr_ratio) > 0:
            feature_row["atr_percentile"] = float(valid_atr_ratio.rank(pct=True).iloc[-1])
    feature_row.update(
        {
            "trade_date": str(ts.date()),
            "year": int(ts.year),
            "instrument": str(instrument or "").strip().upper(),
            "timestamp": pd.Timestamp(ts).isoformat(),
            "snapshot_id": _snapshot_id_from_ts(ts),
            "schema_name": ctx["schema_name"],
            "schema_version": ctx["schema_version"],
            "build_source": "live",
            "build_run_id": str(build_run_id or "").strip(),
            "is_high_vix_day": _resolve_high_vix_flag(vix_live_current, feature_row.get("is_high_vix_day")),
        }
    )

    mapped: Dict[str, Any] = {}
    field_types = dict(ctx["field_types"])
    for legacy_name, new_name in dict(ctx["rename_map"]).items():
        mapped[new_name] = _coerce_contract_value(new_name, feature_row.get(legacy_name), field_types)
    for col in ctx["required_columns"]:
        mapped.setdefault(col, None)

    if validate:
        ctx["validate_snapshot_ml_flat_rows"](
            [mapped],
            contract_dir=contract_dir,
            raise_on_error=True,
        )
    mapped.update(
        _build_runtime_compat_payload(
            feature_row=feature_row,
            panel=panel,
            chain=chain,
            vix_live_current=vix_live_current,
            ts=ts,
        )
    )
    for key in (
        "dealer_proxy_oi_imbalance",
        "dealer_proxy_oi_imbalance_change_5m",
        "dealer_proxy_pcr_change_5m",
        "dealer_proxy_atm_oi_velocity_5m",
        "dealer_proxy_volume_imbalance",
    ):
        if key in feature_row:
            mapped[key] = _coerce_contract_value(key, feature_row.get(key), field_types={})
    return mapped


class LiveSnapshotMLFlatBuilder(LiveMarketSnapshotBuilder):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.flat_state = SnapshotMLFlatState()

    def fetch_spot_ohlc(self, limit: int = 1800) -> pd.DataFrame:
        params = {"timeframe": "1m", "limit": int(limit), "order": "asc"}
        endpoints = (
            "{base}/api/v1/market/ohlc/{symbol}",
            "{base}/api/v1/ohlc/{symbol}",
            "{base}/api/market-data/ohlc/{symbol}",
        )
        for symbol in _spot_symbol_candidates(self.instrument):
            for template in endpoints:
                if "/api/market-data/" in template:
                    base_url = self.dashboard_api_base
                else:
                    base_url = self.market_api_base
                url = template.format(base=base_url, symbol=symbol)
                try:
                    payload = self._get_json(url, params=params)
                    if isinstance(payload, list):
                        frame = _normalize_ohlc_frame(pd.DataFrame(payload))
                        if len(frame):
                            return frame
                except Exception:
                    continue
        return pd.DataFrame()

    def build_snapshot_ml_flat(
        self,
        *,
        ohlc_limit: int = 1800,
        build_run_id: str,
        contract_dir: Optional[Path] = None,
        validate: bool = True,
    ) -> Dict[str, Any]:
        fut_ohlc = self.fetch_ohlc(limit=int(ohlc_limit))
        if len(fut_ohlc) == 0:
            raise RuntimeError(f"no OHLC bars available for {self.instrument}")
        fut_ohlc = self._augment_ohlc_with_kite_history(ohlc=fut_ohlc)
        spot_ohlc = self.fetch_spot_ohlc(limit=max(int(ohlc_limit), len(fut_ohlc)))
        chain = self.fetch_options_chain()
        vix_live = self.fetch_live_vix()
        return build_snapshot_ml_flat_from_inputs(
            instrument=self.instrument,
            fut_ohlc=fut_ohlc,
            spot_ohlc=spot_ohlc,
            chain=chain,
            state=self.flat_state,
            build_run_id=build_run_id,
            vix_live_current=vix_live,
            contract_dir=contract_dir,
            validate=validate,
        )
