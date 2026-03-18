"""Pure-ML dual-side inference helpers for strategy runtime."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import joblib
import numpy as np
import pandas as pd
import json

from .snapshot_accessor import SnapshotAccessor


@dataclass(frozen=True)
class PureMLThresholds:
    ce: float
    pe: float


@dataclass(frozen=True)
class PureMLDecision:
    action: str
    ce_prob: float
    pe_prob: float
    confidence: float
    margin: float
    reason: str


def _safe_float(value: object) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(parsed):
        return None
    return float(parsed)


def _predict_proba_quiet(model: object, x: pd.DataFrame) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="X does not have valid feature names, but LGBMClassifier was fitted with feature names",
            category=UserWarning,
        )
        return model.predict_proba(x)


def load_model_package(path: str | Path) -> dict[str, object]:
    package = joblib.load(Path(path))
    if not isinstance(package, dict):
        raise ValueError("pure ml model package must be dict")
    if str(package.get("kind") or "").strip() == "ml_pipeline_2_staged_runtime_bundle_v1":
        return package
    feature_columns = package.get("feature_columns")
    if not isinstance(feature_columns, list) or len(feature_columns) == 0:
        raise ValueError("pure ml model package missing feature_columns")

    models = package.get("models")
    if isinstance(models, dict) and ("ce" in models) and ("pe" in models):
        return package

    ce_model = package.get("ce_model")
    pe_model = package.get("pe_model")
    if ce_model is None or pe_model is None:
        raise ValueError("pure ml model package must contain models.ce/models.pe or ce_model/pe_model")
    package["models"] = {"ce": ce_model, "pe": pe_model}
    return package


def _resolve_threshold_from_payload(payload: dict[str, Any], side: str) -> Optional[float]:
    key = str(side or "").strip().lower()
    if key not in {"ce", "pe"}:
        return None
    direct = _safe_float(payload.get(f"{key}_threshold"))
    if direct is not None:
        return direct
    dual = payload.get("dual_mode_policy") if isinstance(payload.get("dual_mode_policy"), dict) else {}
    dual_value = _safe_float(dual.get(f"{key}_threshold"))
    if dual_value is not None:
        return dual_value
    util = payload.get("trading_utility_config") if isinstance(payload.get("trading_utility_config"), dict) else {}
    util_value = _safe_float(util.get(f"{key}_threshold"))
    if util_value is not None:
        return util_value
    side_block = payload.get(key) if isinstance(payload.get(key), dict) else {}
    selected = _safe_float(side_block.get("selected_threshold"))
    return selected


def load_thresholds(path: str | Path) -> PureMLThresholds:
    payload_dict = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload_dict, dict):
        raise ValueError("pure ml threshold report payload invalid")

    ce = _resolve_threshold_from_payload(payload_dict, "ce")
    pe = _resolve_threshold_from_payload(payload_dict, "pe")
    if ce is None or pe is None:
        raise ValueError("pure ml threshold report missing ce/pe thresholds")
    return PureMLThresholds(ce=float(ce), pe=float(pe))


def apply_threshold_overrides(
    base: PureMLThresholds,
    *,
    ce_override: Optional[float],
    pe_override: Optional[float],
) -> PureMLThresholds:
    ce = float(ce_override) if ce_override is not None else float(base.ce)
    pe = float(pe_override) if pe_override is not None else float(base.pe)
    if not (0.0 < ce < 1.0):
        raise ValueError("pure ml ce threshold must be in (0,1)")
    if not (0.0 < pe < 1.0):
        raise ValueError("pure ml pe threshold must be in (0,1)")
    return PureMLThresholds(ce=ce, pe=pe)


def infer_action(
    *,
    ce_prob: float,
    pe_prob: float,
    ce_threshold: float,
    pe_threshold: float,
    min_edge: float = 0.0,
) -> tuple[str, str]:
    ce_ok = float(ce_prob) >= float(ce_threshold)
    pe_ok = float(pe_prob) >= float(pe_threshold)
    edge = float(abs(float(ce_prob) - float(pe_prob)))
    if ce_ok and pe_ok:
        if edge < float(min_edge):
            return "HOLD", "low_edge_conflict"
        if float(ce_prob) >= float(pe_prob):
            return "BUY_CE", "dual_pass_ce_higher"
        return "BUY_PE", "dual_pass_pe_higher"
    if ce_ok:
        return "BUY_CE", "ce_above_threshold"
    if pe_ok:
        return "BUY_PE", "pe_above_threshold"
    return "HOLD", "below_threshold"


def build_snapshot_feature_row(snap: SnapshotAccessor) -> dict[str, object]:
    raw_payload = dict(snap.raw_payload)
    feature_row: dict[str, object] = {}
    for key, value in raw_payload.items():
        if isinstance(value, (dict, list, tuple, set)):
            continue
        feature_row[str(key)] = value

    ts = snap.timestamp
    minute_of_day = ((ts.hour * 60 + ts.minute) if isinstance(ts, datetime) else np.nan)
    day_of_week = (ts.weekday() if isinstance(ts, datetime) else np.nan)
    ema_spread = None
    if snap.ema_9 is not None and snap.ema_21 is not None:
        ema_spread = float(snap.ema_9 - snap.ema_21)
    ce_pe_oi_diff = None
    if snap.total_ce_oi is not None and snap.total_pe_oi is not None:
        ce_pe_oi_diff = float(snap.total_ce_oi - snap.total_pe_oi)
    ce_pe_volume_diff = None
    if snap.atm_ce_volume is not None and snap.atm_pe_volume is not None:
        ce_pe_volume_diff = float(snap.atm_ce_volume - snap.atm_pe_volume)
    options_volume_total = None
    if snap.atm_ce_volume is not None and snap.atm_pe_volume is not None:
        options_volume_total = float(snap.atm_ce_volume + snap.atm_pe_volume)
    atm_iv = None
    if snap.atm_ce_iv is not None and snap.atm_pe_iv is not None:
        atm_iv = float((snap.atm_ce_iv + snap.atm_pe_iv) / 2.0)
    regime_vol_high = 1.0 if ((snap.vix_current is not None and snap.vix_current >= 20.0) or (snap.realized_vol_30m is not None and snap.realized_vol_30m >= 0.015)) else 0.0
    regime_vol_low = 1.0 if (snap.vix_current is not None and snap.vix_current <= 12.0) else 0.0
    regime_vol_neutral = 1.0 if (regime_vol_high == 0.0 and regime_vol_low == 0.0) else 0.0
    trend_up = 1.0 if (snap.fut_return_5m is not None and snap.fut_return_15m is not None and snap.fut_return_5m > 0 and snap.fut_return_15m > 0) else 0.0
    trend_down = 1.0 if (snap.fut_return_5m is not None and snap.fut_return_15m is not None and snap.fut_return_5m < 0 and snap.fut_return_15m < 0) else 0.0
    is_near_expiry = 1.0 if (snap.days_to_expiry is not None and snap.days_to_expiry <= 1) else 0.0
    feature_row.update({
        "timestamp": ts,
        "ret_1m": np.nan,
        "ret_3m": np.nan,
        "ret_5m": snap.fut_return_5m,
        "vwap_distance": snap.price_vs_vwap,
        "distance_from_day_high": np.nan,
        "distance_from_day_low": np.nan,
        "opening_range_breakout_up": 1.0 if snap.orh_broken else 0.0,
        "opening_range_breakout_down": 1.0 if snap.orl_broken else 0.0,
        "rsi_14": np.nan,
        "ema_9_21_spread": ema_spread,
        "atr_ratio": snap.realized_vol_30m,
        "atm_call_return_1m": np.nan,
        "atm_put_return_1m": np.nan,
        "atm_oi_change_1m": np.nan,
        "pcr_oi": snap.pcr,
        "ce_pe_oi_diff": ce_pe_oi_diff,
        "ce_pe_volume_diff": ce_pe_volume_diff,
        "fut_rel_volume_20": snap.fut_volume_ratio,
        "fut_volume_accel_1m": np.nan,
        "fut_oi_change_1m": np.nan,
        "fut_oi_change_5m": np.nan,
        "fut_oi_rel_20": np.nan,
        "fut_oi_zscore_20": np.nan,
        "options_volume_total": options_volume_total,
        "options_rel_volume_20": snap.vol_ratio,
        "minute_of_day": minute_of_day,
        "day_of_week": day_of_week,
        "dte_days": snap.days_to_expiry,
        "is_expiry_day": 1.0 if snap.is_expiry_day else 0.0,
        "is_near_expiry": is_near_expiry,
        "vix_prev_close": snap.vix_prev_close,
        "vix_prev_close_change_1d": np.nan,
        "vix_prev_close_zscore_20d": np.nan,
        "is_high_vix_day": 1.0 if (snap.vix_current is not None and snap.vix_current >= 20.0) else 0.0,
        "regime_vol_high": regime_vol_high,
        "regime_vol_low": regime_vol_low,
        "regime_vol_neutral": regime_vol_neutral,
        "regime_atr_high": 1.0 if (snap.realized_vol_30m is not None and snap.realized_vol_30m >= 0.015) else 0.0,
        "regime_atr_low": 1.0 if (snap.realized_vol_30m is not None and snap.realized_vol_30m <= 0.005) else 0.0,
        "regime_trend_up": trend_up,
        "regime_trend_down": trend_down,
        "regime_expiry_near": is_near_expiry,
        "atr_daily_percentile": np.nan,
        "atm_iv": atm_iv,
        "iv_skew": snap.iv_skew,
        # Optional compatibility aliases
        "fut_return_5m": snap.fut_return_5m,
        "fut_return_15m": snap.fut_return_15m,
        "fut_return_30m": snap.fut_return_30m,
        "realized_vol_30m": snap.realized_vol_30m,
        "vol_ratio": snap.vol_ratio,
        "fut_oi": snap.fut_oi,
        "fut_oi_change_30m": snap.fut_oi_change_30m,
        "pcr": snap.pcr,
        "days_to_expiry": snap.days_to_expiry,
        "price_vs_vwap": snap.price_vs_vwap,
        "or_width": snap.or_width,
        "price_vs_orh": snap.price_vs_orh,
        "price_vs_orl": snap.price_vs_orl,
        "atm_ce_close": snap.atm_ce_close,
        "atm_pe_close": snap.atm_pe_close,
        "atm_ce_vol_ratio": snap.atm_ce_vol_ratio,
        "atm_pe_vol_ratio": snap.atm_pe_vol_ratio,
        "atm_ce_oi_change_30m": snap.atm_ce_oi_change_30m,
        "atm_pe_oi_change_30m": snap.atm_pe_oi_change_30m,
    })

    aliases = {
        "time_minute_of_day": feature_row.get("time_minute_of_day", minute_of_day),
        "minute_of_day": feature_row.get("minute_of_day", minute_of_day),
        "time_day_of_week": feature_row.get("time_day_of_week", day_of_week),
        "day_of_week": feature_row.get("day_of_week", day_of_week),
        "time_minute_index": feature_row.get("time_minute_index", snap.minutes_since_open),
        "minute_index": feature_row.get("minute_index", snap.minutes_since_open),
        "ctx_dte_days": feature_row.get("ctx_dte_days", snap.days_to_expiry),
        "dte_days": feature_row.get("dte_days", snap.days_to_expiry),
        "ctx_is_expiry_day": feature_row.get("ctx_is_expiry_day", 1.0 if snap.is_expiry_day else 0.0),
        "is_expiry_day": feature_row.get("is_expiry_day", 1.0 if snap.is_expiry_day else 0.0),
        "ctx_is_near_expiry": feature_row.get("ctx_is_near_expiry", is_near_expiry),
        "is_near_expiry": feature_row.get("is_near_expiry", is_near_expiry),
        "ctx_is_high_vix_day": feature_row.get("ctx_is_high_vix_day", 1.0 if (snap.vix_current is not None and snap.vix_current >= 20.0) else 0.0),
        "is_high_vix_day": feature_row.get("is_high_vix_day", 1.0 if (snap.vix_current is not None and snap.vix_current >= 20.0) else 0.0),
        "ctx_opening_range_ready": feature_row.get("ctx_opening_range_ready", 1.0 if snap.or_ready else 0.0),
        "opening_range_ready": feature_row.get("opening_range_ready", 1.0 if snap.or_ready else 0.0),
        "ctx_opening_range_breakout_up": feature_row.get("ctx_opening_range_breakout_up", 1.0 if snap.orh_broken else 0.0),
        "opening_range_breakout_up": feature_row.get("opening_range_breakout_up", 1.0 if snap.orh_broken else 0.0),
        "ctx_opening_range_breakout_down": feature_row.get("ctx_opening_range_breakout_down", 1.0 if snap.orl_broken else 0.0),
        "opening_range_breakout_down": feature_row.get("opening_range_breakout_down", 1.0 if snap.orl_broken else 0.0),
        "pcr_oi": feature_row.get("pcr_oi", snap.pcr),
        "opt_flow_pcr_oi": feature_row.get("opt_flow_pcr_oi", snap.pcr),
        "ctx_regime_atr_high": feature_row.get("ctx_regime_atr_high", 1.0 if feature_row.get("regime_atr_high") else 0.0),
        "ctx_regime_atr_low": feature_row.get("ctx_regime_atr_low", 1.0 if feature_row.get("regime_atr_low") else 0.0),
        "ctx_regime_trend_up": feature_row.get("ctx_regime_trend_up", trend_up),
        "ctx_regime_trend_down": feature_row.get("ctx_regime_trend_down", trend_down),
        "ctx_regime_expiry_near": feature_row.get("ctx_regime_expiry_near", is_near_expiry),
    }
    for key, value in aliases.items():
        feature_row.setdefault(key, value)

    ce_oi = _safe_float(feature_row.get("opt_flow_ce_oi_total") or feature_row.get("ce_oi_total"))
    pe_oi = _safe_float(feature_row.get("opt_flow_pe_oi_total") or feature_row.get("pe_oi_total"))
    pcr = _safe_float(feature_row.get("opt_flow_pcr_oi") or feature_row.get("pcr_oi"))
    atm_oi_change = _safe_float(feature_row.get("opt_flow_atm_oi_change_1m") or feature_row.get("atm_oi_change_1m"))
    ce_volume_total = _safe_float(feature_row.get("opt_flow_ce_volume_total") or feature_row.get("ce_volume_total"))
    pe_volume_total = _safe_float(feature_row.get("opt_flow_pe_volume_total") or feature_row.get("pe_volume_total"))
    ce_pe_volume_diff = _safe_float(feature_row.get("opt_flow_ce_pe_volume_diff") or feature_row.get("ce_pe_volume_diff"))
    dte_days = _safe_float(feature_row.get("ctx_dte_days") or feature_row.get("dte_days"))
    high_vix = _safe_float(feature_row.get("ctx_is_high_vix_day") or feature_row.get("is_high_vix_day"))

    expiry_weight = 1.0 / (1.0 + float(dte_days or 0.0))
    vix_weight = 1.0 + (0.25 * float(high_vix or 0.0))
    oi_total = (abs(float(ce_oi or 0.0)) + abs(float(pe_oi or 0.0))) or np.nan
    volume_total = (abs(float(ce_volume_total or 0.0)) + abs(float(pe_volume_total or 0.0))) or np.nan

    feature_row.setdefault(
        "dealer_proxy_oi_imbalance",
        (((float(ce_oi or 0.0) - float(pe_oi or 0.0)) / oi_total) * expiry_weight * vix_weight) if np.isfinite(oi_total) and oi_total != 0 else np.nan,
    )
    feature_row.setdefault("dealer_proxy_oi_imbalance_change_5m", 0.0)
    feature_row.setdefault("dealer_proxy_pcr_change_5m", (float(pcr or 0.0) * vix_weight) if pcr is not None else np.nan)
    feature_row.setdefault("dealer_proxy_atm_oi_velocity_5m", (float(atm_oi_change or 0.0) * expiry_weight) if atm_oi_change is not None else np.nan)
    feature_row.setdefault(
        "dealer_proxy_volume_imbalance",
        ((float(ce_pe_volume_diff or 0.0) / volume_total) * vix_weight) if np.isfinite(volume_total) and volume_total != 0 else np.nan,
    )
    return feature_row


def predict_dual(
    *,
    model_package: dict[str, object],
    thresholds: PureMLThresholds,
    feature_row: dict[str, object],
    min_edge: float = 0.0,
) -> PureMLDecision:
    feature_columns = [str(col) for col in list(model_package.get("feature_columns") or [])]
    if not feature_columns:
        raise ValueError("pure ml model package missing feature_columns")
    frame = pd.DataFrame([{col: feature_row.get(col, np.nan) for col in feature_columns}])
    models = model_package.get("models")
    if not isinstance(models, dict) or ("ce" not in models) or ("pe" not in models):
        raise ValueError("pure ml model package missing models.ce/models.pe")
    ce_prob = float(_predict_proba_quiet(models["ce"], frame)[:, 1][0])
    pe_prob = float(_predict_proba_quiet(models["pe"], frame)[:, 1][0])
    action, reason = infer_action(
        ce_prob=ce_prob,
        pe_prob=pe_prob,
        ce_threshold=float(thresholds.ce),
        pe_threshold=float(thresholds.pe),
        min_edge=float(min_edge),
    )
    return PureMLDecision(
        action=action,
        ce_prob=ce_prob,
        pe_prob=pe_prob,
        confidence=float(max(ce_prob, pe_prob)),
        margin=float(abs(ce_prob - pe_prob)),
        reason=str(reason),
    )
