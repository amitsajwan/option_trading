import argparse
import json
import os
import re
import time
import warnings
from datetime import datetime, timedelta, timezone
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import joblib
import numpy as np
import pandas as pd
import requests
import redis
from .pipeline_layout import resolve_vix_source
from .vix_data import load_vix_daily
from .vix_auto_fetch import ensure_vix_history_for_trade_day
from .canonical_event_builder import (
    apply_option_change_features,
    build_canonical_event_from_ohlc_and_chain,
    build_vix_snapshot_for_trade_date,
    extract_option_slice_from_chain,
)
from .feature.regime import attach_regime_features

IST = timezone(timedelta(hours=5, minutes=30))

# Compatibility for model packages persisted when classes were resolved under
# ml_pipeline.live_inference_adapter during training-time execution contexts.
try:
    from .training_cycle import ConstantProbModel as _ConstantProbModel, QuantileClipper as _QuantileClipper

    globals()["QuantileClipper"] = _QuantileClipper
    globals()["ConstantProbModel"] = _ConstantProbModel
except Exception:
    # Keep runtime resilient if training-only dependencies are unavailable.
    pass


@dataclass(frozen=True)
class DecisionThresholds:
    ce: float
    pe: float
    cost_per_trade: float


class _ConstantProbModel:
    def __init__(self, prob: float = 0.0):
        self._prob = float(max(0.0, min(1.0, prob)))

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = int(len(x))
        p1 = np.full(shape=(n,), fill_value=self._prob, dtype=float)
        p0 = 1.0 - p1
        return np.column_stack([p0, p1])


def _predict_proba_quiet(model: object, x: pd.DataFrame) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="X does not have valid feature names, but LGBMClassifier was fitted with feature names",
            category=UserWarning,
        )
        return model.predict_proba(x)


_CONTRACT_WARNED_SIGNATURES: Set[str] = set()


def _safe_float(value: object) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _safe_positive_price(value: object) -> float:
    px = _safe_float(value)
    if np.isfinite(px) and px > 0:
        return float(px)
    return float("nan")


def _normalize_expiry_code(value: object) -> Optional[str]:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if len(text) == 7 and text[:2].isdigit() and text[-2:].isdigit():
        return text
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").strftime("%d%b%y").upper()
        except Exception:
            return None
    if "-" in text:
        try:
            return datetime.fromisoformat(text).strftime("%d%b%y").upper()
        except Exception:
            return None
    return None


def _underlying_symbol_from_instrument(value: object) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return ""
    m = re.match(r"^([A-Z]+)\d{2}[A-Z]{3}(?:\d+)?(?:FUT|CE|PE)$", text)
    if m:
        return str(m.group(1))
    if "-" in text:
        return str(text.split("-", 1)[0])
    cleaned = re.sub(r"[^A-Z]", "", text)
    return cleaned or text


def _derive_event_side(event: Dict[str, object]) -> Optional[str]:
    pos = event.get("position")
    if isinstance(pos, dict):
        side = str(pos.get("side") or "").upper().strip()
        if side in {"CE", "PE"}:
            return side
    side = str(event.get("side") or "").upper().strip()
    if side in {"CE", "PE"}:
        return side
    action = str(event.get("action") or "").upper().strip()
    if action == "BUY_CE":
        return "CE"
    if action == "BUY_PE":
        return "PE"
    return None


def _derive_option_symbol(
    *,
    instrument: object,
    expiry_code: object,
    strike: object,
    side: Optional[str],
) -> Optional[str]:
    side_txt = str(side or "").upper().strip()
    if side_txt not in {"CE", "PE"}:
        return None
    expiry = _normalize_expiry_code(expiry_code)
    strike_num = pd.to_numeric(pd.Series([strike]), errors="coerce").iloc[0]
    if (not expiry) or pd.isna(strike_num):
        return None
    underlying = _underlying_symbol_from_instrument(instrument)
    if not underlying:
        return None
    strike_i = int(round(float(strike_num)))
    return f"{underlying}{expiry}{strike_i}{side_txt}"


def _build_runtime_from_row(
    *,
    row: Optional[Dict[str, object]],
    side: Optional[str],
    instrument_hint: Optional[str],
    entry_price: object = None,
) -> Dict[str, object]:
    src = row if isinstance(row, dict) else {}
    instrument = str(
        src.get("fut_symbol")
        or src.get("instrument")
        or instrument_hint
        or ""
    ).strip().upper()
    expiry_code = _normalize_expiry_code(src.get("expiry_code"))
    strike = pd.to_numeric(pd.Series([src.get("atm_strike")]), errors="coerce").iloc[0]
    atm_strike = int(round(float(strike))) if pd.notna(strike) else None
    option_symbol = _derive_option_symbol(
        instrument=instrument,
        expiry_code=expiry_code,
        strike=atm_strike,
        side=side,
    )

    out: Dict[str, object] = {
        "instrument": (instrument or None),
        "side": (str(side).upper() if side else None),
        "option_symbol": option_symbol,
        "atm_strike": atm_strike,
        "expiry_code": expiry_code,
        "qty": pd.to_numeric(pd.Series([src.get("qty")]), errors="coerce").iloc[0],
        "lots_equivalent": pd.to_numeric(pd.Series([src.get("lots_equivalent")]), errors="coerce").iloc[0],
        "lot_size": pd.to_numeric(pd.Series([src.get("lot_size")]), errors="coerce").iloc[0],
        "entry_price": _safe_positive_price(entry_price),
    }
    for k in ("qty", "lots_equivalent", "lot_size", "entry_price"):
        val = out.get(k)
        if pd.isna(val) if isinstance(val, (float, np.floating)) else False:
            out[k] = None
    return out


def _attach_event_context(
    *,
    event: Dict[str, object],
    position: Optional[Dict[str, object]],
    row: Optional[Dict[str, object]],
    instrument_hint: Optional[str],
) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
    out = dict(event)
    def _missing(value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return len(value.strip()) == 0
        try:
            return bool(pd.isna(value))
        except Exception:
            return False

    side = _derive_event_side(out)
    pos = out.get("position")
    if isinstance(pos, dict):
        pos = dict(pos)
    else:
        pos = None

    runtime = out.get("position_runtime")
    if isinstance(runtime, dict):
        runtime = dict(runtime)
    else:
        runtime = {}

    # Backward-compat: treat legacy "position" payload as runtime carrier.
    if not runtime and isinstance(pos, dict):
        runtime = {
            "side": pos.get("side"),
            "entry_price": pos.get("entry_price"),
            "option_symbol": pos.get("option_symbol"),
            "atm_strike": pos.get("atm_strike"),
            "expiry_code": pos.get("expiry_code"),
            "qty": pos.get("qty"),
            "lots_equivalent": pos.get("lots_equivalent"),
            "lot_size": pos.get("lot_size"),
            "instrument": pos.get("instrument"),
        }

    entry_price = None
    if isinstance(pos, dict):
        entry_price = pos.get("entry_price")
    if entry_price is None and isinstance(position, dict):
        entry_price = position.get("entry_price")

    row_runtime = _build_runtime_from_row(
        row=row,
        side=side,
        instrument_hint=instrument_hint,
        entry_price=entry_price,
    )
    for key, val in row_runtime.items():
        if _missing(runtime.get(key)):
            runtime[key] = val
    if side and not runtime.get("side"):
        runtime["side"] = side

    if runtime:
        out["position_runtime"] = runtime
        out["option_symbol"] = out.get("option_symbol") or runtime.get("option_symbol")
        if not out.get("contract"):
            out["contract"] = out.get("option_symbol")
        out["atm_strike"] = out.get("atm_strike") if out.get("atm_strike") is not None else runtime.get("atm_strike")
        out["expiry_code"] = out.get("expiry_code") or runtime.get("expiry_code")
        out["instrument"] = out.get("instrument") or runtime.get("instrument") or instrument_hint
        if isinstance(pos, dict):
            for key in ("option_symbol", "atm_strike", "expiry_code", "qty", "lots_equivalent", "lot_size", "instrument"):
                if pos.get(key) is None and runtime.get(key) is not None:
                    pos[key] = runtime.get(key)
            out["position"] = pos

    if side and not out.get("side"):
        out["side"] = side
    if not out.get("instrument"):
        out["instrument"] = instrument_hint

    if isinstance(row, dict):
        prices = out.get("prices")
        if not isinstance(prices, dict):
            prices = {}
        for key in ("opt_0_ce_close", "opt_0_pe_close", "fut_close", "spot_close"):
            val = _safe_float(row.get(key))
            if np.isfinite(val):
                prices[key] = float(val)
        if prices:
            out["prices"] = prices

    next_position = position
    if isinstance(next_position, dict) and runtime:
        next_position = dict(next_position)
        for key in ("option_symbol", "atm_strike", "expiry_code", "qty", "lots_equivalent", "lot_size", "instrument"):
            if next_position.get(key) is None and runtime.get(key) is not None:
                next_position[key] = runtime.get(key)
        if side and not next_position.get("side"):
            next_position["side"] = side

    return out, next_position


def _compute_stagnation_metrics(
    *,
    recent_prices: Sequence[float],
    entry_price: float,
    base_threshold_pct: float,
    volatility_multiplier: float,
) -> Dict[str, float]:
    base = max(0.0, float(base_threshold_pct))
    if (not np.isfinite(entry_price)) or float(entry_price) <= 0:
        return {
            "range_pct": float("nan"),
            "adaptive_threshold_pct": float(base),
            "volatility_floor_pct": float("nan"),
            "median_step_pct": float("nan"),
        }
    clean = [float(x) for x in recent_prices if np.isfinite(float(x)) and float(x) > 0]
    if len(clean) < 2:
        return {
            "range_pct": float("nan"),
            "adaptive_threshold_pct": float(base),
            "volatility_floor_pct": float("nan"),
            "median_step_pct": float("nan"),
        }
    arr = np.asarray(clean, dtype=float)
    range_pct = float((float(np.nanmax(arr)) - float(np.nanmin(arr))) / float(entry_price))
    steps = np.abs(np.diff(arr)) / float(entry_price)
    finite_steps = steps[np.isfinite(steps)]
    median_step_pct = float(np.nanmedian(finite_steps)) if finite_steps.size > 0 else float("nan")
    vol_floor = float("nan")
    if np.isfinite(median_step_pct) and float(volatility_multiplier) > 0:
        vol_floor = float(float(volatility_multiplier) * median_step_pct)
    adaptive = float(max(base, vol_floor)) if np.isfinite(vol_floor) else float(base)
    return {
        "range_pct": float(range_pct),
        "adaptive_threshold_pct": float(adaptive),
        "volatility_floor_pct": float(vol_floor),
        "median_step_pct": float(median_step_pct),
    }


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def _extract_list(payload: object, keys: Sequence[str]) -> List[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _extract_dict(payload: object, keys: Sequence[str]) -> Dict[str, object]:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload
    return {}


def _normalize_timestamp_string(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    # Some upstream payloads include both timezone offset and trailing Z.
    if text.endswith("Z") and ("+" in text[10:] or "-" in text[10:]):
        text = text[:-1]
    # Normalize +0530 style offsets to +05:30.
    if len(text) >= 5 and (text[-5] in "+-") and text[-3] != ":" and text[-4:].isdigit():
        text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
    return text


def load_model_package(path: Path) -> Dict[str, object]:
    package = joblib.load(path)
    if not isinstance(package, dict):
        raise ValueError("model package must be dict")
    if "feature_columns" not in package:
        raise ValueError("invalid model package format: missing feature_columns")

    # Legacy package format:
    # { "models": {"ce": <model>, "pe": <model>}, "feature_columns": [...] }
    models = package.get("models")
    if isinstance(models, dict) and ("ce" in models) and ("pe" in models):
        package["_model_package_path"] = str(path)
        package["_model_input_contract"] = load_model_input_contract(path=path, model_package=package)
        return package

    # Modeling-v2 package format:
    # { "ce_model": <model>, "pe_model": <model>, "feature_columns": [...], ... }
    ce_model = package.get("ce_model")
    pe_model = package.get("pe_model")
    if ce_model is not None or pe_model is not None:
        package["models"] = {
            "ce": ce_model if ce_model is not None else _ConstantProbModel(0.0),
            "pe": pe_model if pe_model is not None else _ConstantProbModel(0.0),
        }
    if "models" not in package:
        raise ValueError("invalid model package format: expected models or ce_model/pe_model")
    package["_model_package_path"] = str(path)
    package["_model_input_contract"] = load_model_input_contract(path=path, model_package=package)
    return package


def _normalize_missing_policy(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"error", "warn", "ignore"}:
        return raw
    return "error"


def _default_model_input_contract(model_package: Dict[str, object]) -> Dict[str, object]:
    required = [str(col) for col in list(model_package.get("feature_columns", []))]
    return {
        "schema_version": "1.0",
        "source": "model_package.feature_columns",
        "required_features": required,
        "allow_extra_features": True,
        "missing_policy": _normalize_missing_policy(os.getenv("MODEL_CONTRACT_MISSING_POLICY", "error")),
    }


def _resolve_model_contract_path(model_path: Path) -> Optional[Path]:
    group_root = model_path.parent.parent
    candidates = [
        group_root / "model_contract.json",
        group_root / "contract" / "model_contract.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_model_input_contract(path: Path, model_package: Dict[str, object]) -> Dict[str, object]:
    fallback = _default_model_input_contract(model_package)
    contract_path = _resolve_model_contract_path(path)
    if contract_path is None:
        return fallback
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    required = payload.get("required_features")
    if not isinstance(required, list):
        required = fallback["required_features"]
    contract = {
        "schema_version": str(payload.get("schema_version") or fallback["schema_version"]),
        "source": str(payload.get("source") or f"contract_file:{contract_path}"),
        "required_features": [str(x) for x in required],
        "allow_extra_features": bool(payload.get("allow_extra_features", True)),
        "missing_policy": _normalize_missing_policy(payload.get("missing_policy", fallback["missing_policy"])),
    }
    return contract


def _handle_missing_required_features(
    *,
    missing_required: Sequence[str],
    policy: str,
    context: str,
) -> None:
    if len(missing_required) == 0:
        return
    missing_txt = ", ".join(str(x) for x in list(missing_required)[:30])
    if len(missing_required) > 30:
        missing_txt = f"{missing_txt}, ... (+{len(missing_required) - 30} more)"
    message = f"{context}: missing required model input fields ({len(missing_required)}): {missing_txt}"
    if policy == "error":
        raise ValueError(message)
    if policy == "warn":
        signature = f"{context}|{missing_txt}"
        if signature not in _CONTRACT_WARNED_SIGNATURES:
            _CONTRACT_WARNED_SIGNATURES.add(signature)
            print(f"[model_contract][warn] {message}")


def validate_model_input_columns(
    available_columns: Sequence[str],
    model_package: Dict[str, object],
    *,
    context: str,
    missing_policy_override: Optional[str] = None,
) -> Dict[str, object]:
    contract = model_package.get("_model_input_contract") if isinstance(model_package, dict) else None
    if not isinstance(contract, dict):
        contract = _default_model_input_contract(model_package)
    required = [str(x) for x in list(contract.get("required_features", []))]
    available_set = {str(x) for x in available_columns}
    missing_required = [col for col in required if col not in available_set]
    policy = _normalize_missing_policy(missing_policy_override or contract.get("missing_policy", "error"))
    _handle_missing_required_features(
        missing_required=missing_required,
        policy=policy,
        context=context,
    )
    return {
        "required_count": int(len(required)),
        "missing_required_count": int(len(missing_required)),
        "missing_required_features": list(missing_required),
        "missing_policy": policy,
        "contract_source": str(contract.get("source", "unknown")),
    }


def evaluate_row_model_input_quality(
    row: Dict[str, object],
    model_package: Dict[str, object],
    *,
    context: str = "evaluate_row_model_input_quality",
) -> Dict[str, object]:
    validation = validate_model_input_columns(
        list(row.keys()),
        model_package,
        context=context,
        missing_policy_override="ignore",
    )
    contract = model_package.get("_model_input_contract") if isinstance(model_package, dict) else None
    if not isinstance(contract, dict):
        contract = _default_model_input_contract(model_package)
    required = [str(x) for x in list(contract.get("required_features", []))]
    missing_required = set(str(x) for x in list(validation.get("missing_required_features", [])))
    non_finite_required: List[str] = []
    for col in required:
        if col in missing_required:
            continue
        value = pd.to_numeric(pd.Series([row.get(col)]), errors="coerce").iloc[0]
        if pd.isna(value):
            non_finite_required.append(col)
            continue
        try:
            if not np.isfinite(float(value)):
                non_finite_required.append(col)
        except Exception:
            non_finite_required.append(col)
    return {
        "required_count": int(len(required)),
        "missing_required_count": int(len(missing_required)),
        "missing_required_features": sorted(list(missing_required)),
        "non_finite_required_count": int(len(non_finite_required)),
        "non_finite_required_features": list(non_finite_required),
        "contract_source": str(contract.get("source", "unknown")),
        "is_ready": bool((len(missing_required) == 0) and (len(non_finite_required) == 0)),
    }


def build_model_input_frame(
    row: Dict[str, object],
    model_package: Dict[str, object],
    *,
    missing_policy_override: Optional[str] = None,
    context: str = "predict_decision_from_row",
) -> Tuple[pd.DataFrame, Dict[str, object]]:
    work_row = dict(row)
    # Defensive contract completion for live/replay rows:
    # derive expiry-distance + regime flags when base fields exist.
    if "dte_days" not in work_row:
        td = pd.to_datetime(work_row.get("trade_date"), errors="coerce")
        ec = str(work_row.get("expiry_code", "")).strip().upper()
        exp = pd.to_datetime(ec, format="%Y%m%d", errors="coerce")
        if pd.isna(exp):
            exp = pd.to_datetime(ec, format="%d%b%y", errors="coerce")
        if pd.notna(td) and pd.notna(exp):
            dte = int((exp.normalize() - td.normalize()).days)
            work_row["dte_days"] = float(dte) if dte >= 0 else np.nan
    dte_num = pd.to_numeric(pd.Series([work_row.get("dte_days")]), errors="coerce").iloc[0]
    if "is_expiry_day" not in work_row:
        work_row["is_expiry_day"] = float(1.0) if pd.notna(dte_num) and float(dte_num) == 0.0 else float("nan")
    if "is_near_expiry" not in work_row:
        work_row["is_near_expiry"] = (
            float(1.0)
            if pd.notna(dte_num) and 0.0 <= float(dte_num) <= 1.0
            else (float(0.0) if pd.notna(dte_num) else float("nan"))
        )
    if any(
        key not in work_row
        for key in (
            "regime_vol_high",
            "regime_vol_low",
            "regime_atr_high",
            "regime_atr_low",
            "regime_trend_up",
            "regime_trend_down",
            "regime_expiry_near",
        )
    ):
        reg_df = attach_regime_features(pd.DataFrame([work_row]))
        reg_row = reg_df.iloc[0].to_dict()
        for key in (
            "regime_vol_high",
            "regime_vol_low",
            "regime_atr_high",
            "regime_atr_low",
            "regime_trend_up",
            "regime_trend_down",
            "regime_expiry_near",
        ):
            if key not in work_row:
                value = pd.to_numeric(pd.Series([reg_row.get(key)]), errors="coerce").iloc[0]
                work_row[key] = float(value) if pd.notna(value) else float("nan")

    validation = validate_model_input_columns(
        list(work_row.keys()),
        model_package,
        context=context,
        missing_policy_override=missing_policy_override,
    )
    feature_cols = [str(col) for col in list(model_package["feature_columns"])]
    x = pd.DataFrame([{col: work_row.get(col, np.nan) for col in feature_cols}])
    return x, validation


def predict_probabilities_from_frame(
    feature_df: pd.DataFrame,
    model_package: Dict[str, object],
    *,
    missing_policy_override: Optional[str] = None,
    context: str = "predict_probabilities_from_frame",
) -> Tuple[pd.DataFrame, Dict[str, object]]:
    validation = validate_model_input_columns(
        list(feature_df.columns),
        model_package,
        context=context,
        missing_policy_override=missing_policy_override,
    )
    feature_cols = [str(col) for col in list(model_package["feature_columns"])]
    x = feature_df.copy()
    for col in feature_cols:
        if col not in x.columns:
            x[col] = np.nan
    x = x.loc[:, feature_cols]
    models = model_package["models"]
    ce_prob = _predict_proba_quiet(models["ce"], x)[:, 1]
    pe_prob = _predict_proba_quiet(models["pe"], x)[:, 1]
    return pd.DataFrame({"ce_prob": ce_prob, "pe_prob": pe_prob}), validation


def load_thresholds(path: Path) -> DecisionThresholds:
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Legacy T08 format:
    # {
    #   "ce": {"selected_threshold": ...},
    #   "pe": {"selected_threshold": ...},
    #   "decision_config": {"cost_per_trade": ...}
    # }
    ce = payload.get("ce", {}).get("selected_threshold")
    pe = payload.get("pe", {}).get("selected_threshold")

    # T31 calibration format:
    # {
    #   "dual_mode_policy": {"ce_threshold": ..., "pe_threshold": ...},
    #   "decision_config": {"cost_per_trade": ...}
    # }
    if ce is None or pe is None:
        dual = payload.get("dual_mode_policy") or {}
        ce = dual.get("ce_threshold")
        pe = dual.get("pe_threshold")
    # Training-cycle utility report format:
    # {
    #   "trading_utility_config": {
    #     "ce_threshold": ...,
    #     "pe_threshold": ...,
    #     "cost_per_trade": ...
    #   }
    # }
    if ce is None or pe is None:
        utility_cfg = payload.get("trading_utility_config") or {}
        ce = utility_cfg.get("ce_threshold")
        pe = utility_cfg.get("pe_threshold")
    # Modeling-v2 threshold format:
    # {
    #   "ce_threshold": ...,
    #   "pe_threshold": ...,
    #   ...
    # }
    if ce is None or pe is None:
        ce = payload.get("ce_threshold")
        pe = payload.get("pe_threshold")
    if ce is None or pe is None:
        raise ValueError("threshold report missing selected thresholds")
    decision_cfg = payload.get("decision_config") or {}
    utility_cfg = payload.get("trading_utility_config") or {}
    cost = float(decision_cfg.get("cost_per_trade", utility_cfg.get("cost_per_trade", 0.0006)))
    return DecisionThresholds(ce=float(ce), pe=float(pe), cost_per_trade=cost)


def infer_action(ce_prob: float, pe_prob: float, ce_threshold: float, pe_threshold: float, mode: str) -> str:
    ce_ok = ce_prob >= ce_threshold
    pe_ok = pe_prob >= pe_threshold
    if mode == "ce_only":
        return "BUY_CE" if ce_ok else "HOLD"
    if mode == "pe_only":
        return "BUY_PE" if pe_ok else "HOLD"
    if mode != "dual":
        raise ValueError(f"unsupported mode: {mode}")
    if ce_ok and pe_ok:
        return "BUY_CE" if ce_prob >= pe_prob else "BUY_PE"
    if ce_ok:
        return "BUY_CE"
    if pe_ok:
        return "BUY_PE"
    return "HOLD"


def predict_decision_from_row(
    row: Dict[str, object],
    model_package: Dict[str, object],
    thresholds: DecisionThresholds,
    mode: str,
    require_complete_row_inputs: bool = False,
) -> Dict[str, object]:
    row_quality = evaluate_row_model_input_quality(
        row=row,
        model_package=model_package,
        context="predict_decision_from_row",
    )
    if bool(require_complete_row_inputs) and not bool(row_quality.get("is_ready", True)):
        return {
            "generated_at": _now_iso(),
            "timestamp": str(row.get("timestamp")),
            "trade_date": str(row.get("trade_date", "")),
            "mode": mode,
            "ce_prob": float("nan"),
            "pe_prob": float("nan"),
            "ce_threshold": float(thresholds.ce),
            "pe_threshold": float(thresholds.pe),
            "action": "HOLD",
            "confidence": float("nan"),
            "decision_reason": "model_input_incomplete",
            "input_ready": False,
            "input_contract_missing_required_count": int(row_quality.get("missing_required_count", 0)),
            "input_contract_missing_required_features": list(row_quality.get("missing_required_features", [])),
            "input_required_non_finite_count": int(row_quality.get("non_finite_required_count", 0)),
            "input_required_non_finite_features": list(row_quality.get("non_finite_required_features", [])),
            "input_contract_source": str(row_quality.get("contract_source", "")),
        }
    x, validation = build_model_input_frame(
        row=row,
        model_package=model_package,
        context="predict_decision_from_row",
    )
    models = model_package["models"]
    ce_prob = float(_predict_proba_quiet(models["ce"], x)[0, 1])
    pe_prob = float(_predict_proba_quiet(models["pe"], x)[0, 1])
    action = infer_action(ce_prob, pe_prob, thresholds.ce, thresholds.pe, mode=mode)
    confidence = float(max(ce_prob, pe_prob))
    return {
        "generated_at": _now_iso(),
        "timestamp": str(row.get("timestamp")),
        "trade_date": str(row.get("trade_date", "")),
        "mode": mode,
        "ce_prob": ce_prob,
        "pe_prob": pe_prob,
        "ce_threshold": float(thresholds.ce),
        "pe_threshold": float(thresholds.pe),
        "action": action,
        "confidence": confidence,
        "input_ready": bool(row_quality.get("is_ready", True)),
        "input_contract_missing_required_count": int(validation.get("missing_required_count", 0)),
        "input_contract_missing_required_features": list(validation.get("missing_required_features", [])),
        "input_required_non_finite_count": int(row_quality.get("non_finite_required_count", 0)),
        "input_required_non_finite_features": list(row_quality.get("non_finite_required_features", [])),
        "input_contract_source": str(validation.get("contract_source", "")),
    }


def _append_jsonl(path: Path, rows: Sequence[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def run_replay_dry_run(
    feature_parquet: Path,
    model_package: Dict[str, object],
    thresholds: DecisionThresholds,
    output_jsonl: Path,
    mode: str = "dual",
    limit: Optional[int] = None,
) -> Dict[str, object]:
    frame = pd.read_parquet(feature_parquet)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if limit is not None:
        frame = frame.head(int(limit)).copy()

    decisions: List[Dict[str, object]] = []
    for row in frame.to_dict(orient="records"):
        decisions.append(
            predict_decision_from_row(
                row,
                model_package,
                thresholds,
                mode=mode,
                require_complete_row_inputs=True,
            )
        )

    _append_jsonl(output_jsonl, decisions)
    action_counts: Dict[str, int] = {}
    for d in decisions:
        action_counts[d["action"]] = action_counts.get(d["action"], 0) + 1
    return {
        "mode": mode,
        "rows_processed": int(len(frame)),
        "decisions_emitted": int(len(decisions)),
        "action_counts": action_counts,
        "output_jsonl": str(output_jsonl),
    }


def run_replay_dry_run_v2(
    feature_parquet: Path,
    model_package: Dict[str, object],
    thresholds: DecisionThresholds,
    output_jsonl: Path,
    mode: str = "dual",
    limit: Optional[int] = None,
    max_hold_minutes: int = 5,
    confidence_buffer: float = 0.05,
    stagnation_enabled: bool = False,
    stagnation_window_minutes: int = 10,
    stagnation_threshold_pct: float = 0.008,
    stagnation_volatility_multiplier: float = 2.0,
    stagnation_min_hold_minutes: int = 0,
) -> Dict[str, object]:
    frame = pd.read_parquet(feature_parquet)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if limit is not None:
        frame = frame.head(int(limit)).copy()

    events: List[Dict[str, object]] = []
    position: Optional[Dict[str, object]] = None
    for row in frame.to_dict(orient="records"):
        decision = predict_decision_from_row(
            row,
            model_package,
            thresholds,
            mode=mode,
            require_complete_row_inputs=True,
        )
        ce_px = _safe_positive_price(row.get("opt_0_ce_close"))
        pe_px = _safe_positive_price(row.get("opt_0_pe_close"))
        event, position = _emit_exit_aware_event(
            decision=decision,
            position=position,
            thresholds=thresholds,
            max_hold_minutes=int(max_hold_minutes),
            confidence_buffer=float(confidence_buffer),
            current_ce_price=ce_px,
            current_pe_price=pe_px,
            stagnation_enabled=bool(stagnation_enabled),
            stagnation_window_minutes=int(stagnation_window_minutes),
            stagnation_threshold_pct=float(stagnation_threshold_pct),
            stagnation_volatility_multiplier=float(stagnation_volatility_multiplier),
            stagnation_min_hold_minutes=int(stagnation_min_hold_minutes),
        )
        event, position = _attach_event_context(
            event=event,
            position=position,
            row=row,
            instrument_hint=str(row.get("fut_symbol") or row.get("instrument") or ""),
        )
        events.append(event)

    if position is not None and len(frame) > 0:
        session_end = _build_session_end_event(
            mode=mode,
            thresholds=thresholds,
            last_timestamp=pd.Timestamp(frame.iloc[-1]["timestamp"]),
            position=position,
        )
        last_row = frame.iloc[-1].to_dict()
        session_end, position = _attach_event_context(
            event=session_end,
            position=position,
            row=last_row,
            instrument_hint=str(last_row.get("fut_symbol") or last_row.get("instrument") or ""),
        )
        events.append(session_end)
        position = None

    _append_jsonl(output_jsonl, events)
    event_counts: Dict[str, int] = {}
    event_reason_counts: Dict[str, int] = {}
    for item in events:
        key = str(item.get("event_type"))
        event_counts[key] = event_counts.get(key, 0) + 1
        reason = str(item.get("event_reason"))
        event_reason_counts[reason] = event_reason_counts.get(reason, 0) + 1
    return {
        "mode": mode,
        "rows_processed": int(len(frame)),
        "events_emitted": int(len(events)),
        "event_counts": event_counts,
        "event_reason_counts": event_reason_counts,
        "stagnation_exit": {
            "enabled": bool(stagnation_enabled),
            "window_minutes": int(stagnation_window_minutes),
            "threshold_pct": float(stagnation_threshold_pct),
            "volatility_multiplier": float(stagnation_volatility_multiplier),
            "min_hold_minutes": int(stagnation_min_hold_minutes),
        },
        "output_jsonl": str(output_jsonl),
    }


def _emit_exit_aware_event(
    *,
    decision: Dict[str, object],
    position: Optional[Dict[str, object]],
    thresholds: DecisionThresholds,
    max_hold_minutes: int,
    confidence_buffer: float,
    current_ce_price: float = float("nan"),
    current_pe_price: float = float("nan"),
    stagnation_enabled: bool = False,
    stagnation_window_minutes: int = 10,
    stagnation_threshold_pct: float = 0.008,
    stagnation_volatility_multiplier: float = 2.0,
    stagnation_min_hold_minutes: int = 0,
) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
    ts = pd.Timestamp(decision["timestamp"])
    if position is None:
        if decision["action"] == "BUY_CE":
            entry_price = _safe_positive_price(current_ce_price)
            next_position = {"side": "CE", "entry_timestamp": ts.isoformat(), "entry_confidence": decision["confidence"]}
            if np.isfinite(entry_price):
                next_position["entry_price"] = float(entry_price)
                if bool(stagnation_enabled):
                    next_position["stagnation_prices"] = [float(entry_price)]
            return {**decision, "event_type": "ENTRY", "event_reason": "signal_entry", "position": next_position}, next_position
        if decision["action"] == "BUY_PE":
            entry_price = _safe_positive_price(current_pe_price)
            next_position = {"side": "PE", "entry_timestamp": ts.isoformat(), "entry_confidence": decision["confidence"]}
            if np.isfinite(entry_price):
                next_position["entry_price"] = float(entry_price)
                if bool(stagnation_enabled):
                    next_position["stagnation_prices"] = [float(entry_price)]
            return {**decision, "event_type": "ENTRY", "event_reason": "signal_entry", "position": next_position}, next_position
        idle_reason = str(decision.get("decision_reason") or "no_signal")
        return {**decision, "event_type": "IDLE", "event_reason": idle_reason, "position": None}, None

    next_position = dict(position)
    side = str(next_position["side"])
    entry_ts = pd.Timestamp(str(next_position["entry_timestamp"]))
    held_minutes = int((ts - entry_ts) / pd.Timedelta(minutes=1))
    side_prob = float(decision["ce_prob"] if side == "CE" else decision["pe_prob"])
    side_threshold = float(thresholds.ce if side == "CE" else thresholds.pe)
    opposite_action = "BUY_PE" if side == "CE" else "BUY_CE"
    side_price = _safe_positive_price(current_ce_price if side == "CE" else current_pe_price)
    entry_price = _safe_positive_price(next_position.get("entry_price"))
    if np.isfinite(side_price) and (not np.isfinite(entry_price)):
        entry_price = float(side_price)
        next_position["entry_price"] = float(entry_price)

    stagnation_meta: Dict[str, object] = {}
    if bool(stagnation_enabled):
        win = int(max(2, stagnation_window_minutes))
        history_raw = next_position.get("stagnation_prices")
        history: List[float] = []
        if isinstance(history_raw, list):
            history = [float(x) for x in history_raw if np.isfinite(_safe_positive_price(x))]
        if np.isfinite(side_price):
            history.append(float(side_price))
        if len(history) > win:
            history = history[-win:]
        next_position["stagnation_prices"] = history

        metrics = _compute_stagnation_metrics(
            recent_prices=history,
            entry_price=entry_price,
            base_threshold_pct=float(stagnation_threshold_pct),
            volatility_multiplier=float(stagnation_volatility_multiplier),
        )
        stagnation_meta = {
            "stagnation_window_minutes": int(win),
            "stagnation_range_pct": float(metrics["range_pct"]),
            "stagnation_threshold_pct": float(max(0.0, float(stagnation_threshold_pct))),
            "stagnation_adaptive_threshold_pct": float(metrics["adaptive_threshold_pct"]),
            "stagnation_volatility_floor_pct": float(metrics["volatility_floor_pct"]),
            "stagnation_median_step_pct": float(metrics["median_step_pct"]),
        }

    exit_reason = None
    if decision["action"] == opposite_action:
        exit_reason = "signal_flip"
    elif bool(stagnation_enabled):
        win = int(max(2, stagnation_window_minutes))
        min_hold = int(max(0, stagnation_min_hold_minutes))
        history = next_position.get("stagnation_prices")
        enough_bars = isinstance(history, list) and len(history) >= win
        metrics_ready = np.isfinite(_safe_float(stagnation_meta.get("stagnation_range_pct")))
        eligible_hold = int(held_minutes) >= int(max(win - 1, min_hold))
        if enough_bars and eligible_hold and metrics_ready:
            if float(stagnation_meta["stagnation_range_pct"]) <= float(stagnation_meta["stagnation_adaptive_threshold_pct"]):
                exit_reason = "stagnation"
    elif held_minutes >= int(max_hold_minutes):
        exit_reason = "time_stop"
    elif side_prob < max(0.0, side_threshold - float(confidence_buffer)):
        exit_reason = "confidence_fade"

    if exit_reason is not None:
        event = {
            **decision,
            "event_type": "EXIT",
            "event_reason": exit_reason,
            "held_minutes": int(held_minutes),
            "position": next_position,
        }
        if stagnation_meta:
            event.update(stagnation_meta)
        return (
            event,
            None,
        )
    hold_reason = "hold"
    if str(decision.get("decision_reason", "")) == "model_input_incomplete":
        hold_reason = "model_input_incomplete_hold"
    event = {
        **decision,
        "event_type": "MANAGE",
        "event_reason": hold_reason,
        "held_minutes": int(held_minutes),
        "position": next_position,
    }
    if stagnation_meta:
        event.update(stagnation_meta)
    return (
        event,
        next_position,
    )


def _build_session_end_event(
    *,
    mode: str,
    thresholds: DecisionThresholds,
    last_timestamp: pd.Timestamp,
    position: Dict[str, object],
) -> Dict[str, object]:
    last_ts = last_timestamp.isoformat()
    return {
        "generated_at": _now_iso(),
        "timestamp": last_ts,
        "trade_date": str(pd.Timestamp(last_ts).date()),
        "mode": mode,
        "ce_prob": float("nan"),
        "pe_prob": float("nan"),
        "ce_threshold": float(thresholds.ce),
        "pe_threshold": float(thresholds.pe),
        "action": "HOLD",
        "confidence": float("nan"),
        "event_type": "EXIT",
        "event_reason": "session_end",
        "held_minutes": int((pd.Timestamp(last_ts) - pd.Timestamp(position["entry_timestamp"])) / pd.Timedelta(minutes=1)),
        "position": position,
    }


class LiveMarketFeatureClient:
    def __init__(
        self,
        market_api_base: str = "http://127.0.0.1:8004",
        dashboard_api_base: str = "http://127.0.0.1:8002",
        timeout_seconds: float = 5.0,
        vix_source: Optional[str] = None,
    ):
        self.market_api_base = market_api_base.rstrip("/")
        self.dashboard_api_base = dashboard_api_base.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self._prev_trade_date: Optional[str] = None
        self._prev_opt0_ce_close: Optional[float] = None
        self._prev_opt0_pe_close: Optional[float] = None
        self._prev_opt0_total_oi: Optional[float] = None
        auto_vix_source = None
        try:
            auto_vix_source = ensure_vix_history_for_trade_day(
                trade_day=str(pd.Timestamp.now(tz=IST).date()),
            )
        except Exception:
            auto_vix_source = None
        resolved_vix_arg = vix_source if vix_source else auto_vix_source
        self._vix_source = resolve_vix_source(explicit_vix=resolved_vix_arg)
        self._vix_daily = load_vix_daily(self._vix_source) if self._vix_source else pd.DataFrame()

    def _get_json(self, url: str) -> object:
        response = requests.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response.json()

    def fetch_ohlc(self, instrument: str, limit: int = 80) -> pd.DataFrame:
        url = (
            f"{self.market_api_base}/api/v1/market/ohlc/{instrument}"
            f"?timeframe=1min&limit={int(limit)}&order=asc"
        )
        payload = self._get_json(url)
        bars = _extract_list(payload, keys=("data", "ohlc", "bars"))
        if not bars:
            return pd.DataFrame()
        out = pd.DataFrame(bars)
        ts_col = "start_at" if "start_at" in out.columns else ("timestamp" if "timestamp" in out.columns else None)
        if ts_col is None:
            return pd.DataFrame()
        out["timestamp"] = pd.to_datetime(out[ts_col].map(_normalize_timestamp_string), errors="coerce")
        for col in ("open", "high", "low", "close", "volume", "oi"):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        return out

    def fetch_options_chain(self, instrument: str) -> Dict[str, object]:
        url = f"{self.dashboard_api_base}/api/market-data/options/{instrument}"
        payload = self._get_json(url)
        return _extract_dict(payload, keys=("data", "payload"))

    @staticmethod
    def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta.where(delta < 0, 0.0)).abs()
        avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
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

    @staticmethod
    def _vwap(df: pd.DataFrame) -> pd.Series:
        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        pv = typical * df["volume"].fillna(0.0)
        return pv.cumsum() / df["volume"].fillna(0.0).cumsum().replace(0.0, np.nan)

    @staticmethod
    def _extract_option_slice(chain: Dict[str, object], fut_price: float) -> Dict[str, float]:
        return extract_option_slice_from_chain(chain, fut_price=fut_price)

    def build_latest_feature_row(self, instrument: str) -> Dict[str, object]:
        ohlc = self.fetch_ohlc(instrument=instrument, limit=90)
        if len(ohlc) == 0:
            raise RuntimeError("no OHLC data returned from API")
        chain = self.fetch_options_chain(instrument=instrument)
        row = build_live_canonical_event(
            ohlc=ohlc,
            chain=chain,
            options_extractor=self._extract_option_slice,
            rsi_fn=self._rsi,
            atr_fn=self._atr,
            vwap_fn=self._vwap,
            vix_snapshot=_build_live_vix_snapshot(self._vix_daily, pd.Timestamp(ohlc.iloc[-1]["timestamp"]).date()),
        )
        (
            self._prev_trade_date,
            self._prev_opt0_ce_close,
            self._prev_opt0_pe_close,
            self._prev_opt0_total_oi,
        ) = _apply_live_option_change_features(
            row,
            prev_trade_date=self._prev_trade_date,
            prev_opt0_ce_close=self._prev_opt0_ce_close,
            prev_opt0_pe_close=self._prev_opt0_pe_close,
            prev_opt0_total_oi=self._prev_opt0_total_oi,
        )
        return row


def build_live_canonical_event(
    *,
    ohlc: pd.DataFrame,
    chain: Dict[str, object],
    options_extractor: Any,
    rsi_fn: Any,
    atr_fn: Any,
    vwap_fn: Any,
    vix_snapshot: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    _ = (options_extractor, rsi_fn, atr_fn, vwap_fn)  # kept for backward call compatibility
    row = build_canonical_event_from_ohlc_and_chain(
        ohlc=ohlc,
        chain=chain,
        vix_snapshot=vix_snapshot,
    )
    reg_frame = attach_regime_features(pd.DataFrame([row]))
    reg_row = reg_frame.iloc[0].to_dict()
    for key in (
        "regime_vol_high",
        "regime_vol_low",
        "regime_vol_neutral",
        "regime_atr_high",
        "regime_atr_low",
        "regime_trend_up",
        "regime_trend_down",
        "regime_expiry_near",
    ):
        row[key] = reg_row.get(key, float("nan"))
    return row


def _build_feature_row_from_ohlc_and_chain(
    *,
    ohlc: pd.DataFrame,
    chain: Dict[str, object],
    options_extractor: Any,
    rsi_fn: Any,
    atr_fn: Any,
    vwap_fn: Any,
    vix_snapshot: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    # Backward-compatible alias for existing call sites.
    return build_live_canonical_event(
        ohlc=ohlc,
        chain=chain,
        options_extractor=options_extractor,
        rsi_fn=rsi_fn,
        atr_fn=atr_fn,
        vwap_fn=vwap_fn,
        vix_snapshot=vix_snapshot,
    )


def _apply_live_option_change_features(
    row: Dict[str, object],
    *,
    prev_trade_date: Optional[str],
    prev_opt0_ce_close: Optional[float],
    prev_opt0_pe_close: Optional[float],
    prev_opt0_total_oi: Optional[float],
) -> Tuple[Optional[str], Optional[float], Optional[float], Optional[float]]:
    return apply_option_change_features(
        row,
        prev_trade_date=prev_trade_date,
        prev_opt0_ce_close=prev_opt0_ce_close,
        prev_opt0_pe_close=prev_opt0_pe_close,
        prev_opt0_total_oi=prev_opt0_total_oi,
    )


def _build_live_vix_snapshot(vix_daily: pd.DataFrame, trade_date: object) -> Dict[str, float]:
    return build_vix_snapshot_for_trade_date(vix_daily=vix_daily, trade_date=trade_date)


class RedisEventFeatureClient:
    def __init__(
        self,
        instrument: str,
        max_bars: int = 120,
        redis_client: Optional[redis.Redis] = None,
        mode_hint: Optional[str] = None,
        vix_source: Optional[str] = None,
    ):
        self.instrument = str(instrument)
        self.max_bars = int(max(20, max_bars))
        self._ohlc_bars: Deque[Dict[str, object]] = deque(maxlen=self.max_bars)
        self._latest_chain: Optional[Dict[str, object]] = None
        self._latest_depth: Optional[Dict[str, object]] = None
        self._depth_updates: int = 0
        self._redis_client = redis_client
        self._mode_hint = str(mode_hint or "").strip().lower() or None
        self._instrument_keys = [self.instrument, self.instrument.upper()]
        self._prev_trade_date: Optional[str] = None
        self._prev_opt0_ce_close: Optional[float] = None
        self._prev_opt0_pe_close: Optional[float] = None
        self._prev_opt0_total_oi: Optional[float] = None
        auto_vix_source = None
        try:
            auto_vix_source = ensure_vix_history_for_trade_day(
                trade_day=str(pd.Timestamp.now(tz=IST).date()),
            )
        except Exception:
            auto_vix_source = None
        resolved_vix_arg = vix_source if vix_source else auto_vix_source
        self._vix_source = resolve_vix_source(explicit_vix=resolved_vix_arg)
        self._vix_daily = load_vix_daily(self._vix_source) if self._vix_source else pd.DataFrame()

    @staticmethod
    def _decode_json(data: object) -> Optional[Dict[str, object]]:
        if isinstance(data, dict):
            return data
        if isinstance(data, (bytes, bytearray)):
            try:
                data = data.decode("utf-8")
            except Exception:
                return None
        if isinstance(data, str):
            text = data.strip()
            if not text:
                return None
            try:
                decoded = json.loads(text)
            except Exception:
                return None
            return decoded if isinstance(decoded, dict) else None
        return None

    @staticmethod
    def _canonical_timeframe(value: object) -> str:
        tf = str(value or "").strip().lower()
        if tf.endswith("min"):
            digits = tf[:-3]
            if digits.isdigit():
                return f"{digits}m"
        return tf

    @staticmethod
    def _extract_bar_timestamp(bar: Dict[str, object], fallback_event_time: object) -> Optional[pd.Timestamp]:
        raw_ts = bar.get("start_at") or bar.get("timestamp") or fallback_event_time
        parsed = pd.to_datetime(_normalize_timestamp_string(raw_ts), errors="coerce")
        if pd.isna(parsed):
            return None
        return pd.Timestamp(parsed)

    @staticmethod
    def _decode_json_list(data: object) -> List[Dict[str, object]]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, str):
            text = data.strip()
            if not text:
                return []
            try:
                decoded = json.loads(text)
            except Exception:
                return []
            if isinstance(decoded, list):
                return [x for x in decoded if isinstance(x, dict)]
        return []

    @staticmethod
    def _build_depth_snapshot(
        buy_levels: object,
        sell_levels: object,
        timestamp: object,
        total_bid_qty: object = None,
        total_ask_qty: object = None,
    ) -> Dict[str, object]:
        buy = RedisEventFeatureClient._decode_json_list(buy_levels)
        sell = RedisEventFeatureClient._decode_json_list(sell_levels)
        top_buy = buy[0] if buy else {}
        top_sell = sell[0] if sell else {}
        bid_total = _safe_float(total_bid_qty)
        ask_total = _safe_float(total_ask_qty)
        if not np.isfinite(bid_total):
            bid_total = float(np.nansum([_safe_float(x.get("quantity")) for x in buy]))
        if not np.isfinite(ask_total):
            ask_total = float(np.nansum([_safe_float(x.get("quantity")) for x in sell]))
        denom = bid_total + ask_total
        imbalance = float((bid_total - ask_total) / denom) if np.isfinite(denom) and denom > 0 else float("nan")
        top_bid_price = _safe_float(top_buy.get("price"))
        top_ask_price = _safe_float(top_sell.get("price"))
        spread = float(top_ask_price - top_bid_price) if np.isfinite(top_ask_price) and np.isfinite(top_bid_price) else float("nan")
        return {
            "depth_timestamp": _normalize_timestamp_string(timestamp),
            "depth_total_bid_qty": float(bid_total) if np.isfinite(bid_total) else float("nan"),
            "depth_total_ask_qty": float(ask_total) if np.isfinite(ask_total) else float("nan"),
            "depth_top_bid_qty": _safe_float(top_buy.get("quantity")),
            "depth_top_ask_qty": _safe_float(top_sell.get("quantity")),
            "depth_top_bid_price": float(top_bid_price) if np.isfinite(top_bid_price) else float("nan"),
            "depth_top_ask_price": float(top_ask_price) if np.isfinite(top_ask_price) else float("nan"),
            "depth_spread": spread,
            "depth_imbalance": imbalance,
        }

    def _mode_candidates(self) -> List[str]:
        candidates: List[str] = []
        for value in (self._mode_hint, str(os.getenv("EXECUTION_MODE", "")).strip().lower(), "live", "historical", "paper"):
            if value and value not in candidates:
                candidates.append(value)
        return candidates

    def _depth_key_candidates(self, suffix: str) -> List[str]:
        keys: List[str] = []
        for inst in self._instrument_keys:
            for mode in self._mode_candidates():
                keys.append(f"{mode}:depth:{inst}:{suffix}")
            keys.append(f"depth:{inst}:{suffix}")
        return keys

    def _try_redis_get(self, keys: Sequence[str]) -> Optional[object]:
        if self._redis_client is None:
            return None
        for key in keys:
            try:
                value = self._redis_client.get(key)
            except Exception:
                value = None
            if value is not None:
                return value
        return None

    def _refresh_depth_from_redis(self) -> None:
        if self._redis_client is None:
            return
        buy_raw = self._try_redis_get(self._depth_key_candidates("buy"))
        sell_raw = self._try_redis_get(self._depth_key_candidates("sell"))
        ts_raw = self._try_redis_get(self._depth_key_candidates("timestamp"))
        bid_total = self._try_redis_get(self._depth_key_candidates("total_bid_qty"))
        ask_total = self._try_redis_get(self._depth_key_candidates("total_ask_qty"))
        if buy_raw is None and sell_raw is None and ts_raw is None and bid_total is None and ask_total is None:
            return
        snapshot = self._build_depth_snapshot(
            buy_levels=buy_raw if buy_raw is not None else "[]",
            sell_levels=sell_raw if sell_raw is not None else "[]",
            timestamp=ts_raw,
            total_bid_qty=bid_total,
            total_ask_qty=ask_total,
        )
        self._latest_depth = snapshot
        self._depth_updates += 1

    def consume_redis_message(self, msg: Dict[str, object]) -> Optional[str]:
        msg_type = str(msg.get("type", ""))
        if msg_type not in {"message", "pmessage"}:
            return None
        channel = str(msg.get("channel") or "")
        decoded = self._decode_json(msg.get("data"))
        if not decoded:
            return None

        if channel.startswith("market:options:"):
            if isinstance(decoded.get("payload"), dict):
                self._latest_chain = dict(decoded["payload"])
            else:
                self._latest_chain = dict(decoded)
            return None

        if channel.startswith("market:depth:"):
            payload = decoded.get("payload") if isinstance(decoded.get("payload"), dict) else decoded
            if isinstance(payload, dict):
                snapshot = self._build_depth_snapshot(
                    buy_levels=payload.get("buy", []),
                    sell_levels=payload.get("sell", []),
                    timestamp=payload.get("timestamp") or decoded.get("event_time"),
                    total_bid_qty=payload.get("total_bid_qty"),
                    total_ask_qty=payload.get("total_ask_qty"),
                )
                self._latest_depth = snapshot
                self._depth_updates += 1
            return None

        if not channel.startswith("market:ohlc:"):
            return None
        parts = channel.split(":")
        if len(parts) < 4:
            return None
        tf = self._canonical_timeframe(parts[-1])
        if tf != "1m":
            return None

        envelope = decoded
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else envelope
        if not isinstance(payload, dict):
            return None
        if payload.get("candle_closed") is False:
            return None

        ts = self._extract_bar_timestamp(payload, envelope.get("event_time"))
        if ts is None:
            return None
        bar = {
            "timestamp": ts,
            "open": _safe_float(payload.get("open")),
            "high": _safe_float(payload.get("high")),
            "low": _safe_float(payload.get("low")),
            "close": _safe_float(payload.get("close")),
            "volume": _safe_float(payload.get("volume")),
            "oi": _safe_float(payload.get("oi")),
        }
        if len(self._ohlc_bars) > 0:
            prev_ts = pd.Timestamp(self._ohlc_bars[-1]["timestamp"])
            if ts <= prev_ts:
                return None
        self._ohlc_bars.append(bar)
        return ts.isoformat()

    def build_latest_feature_row(self) -> Dict[str, object]:
        if len(self._ohlc_bars) == 0:
            raise RuntimeError("no closed 1m ohlc bars from redis pubsub")
        if self._latest_chain is None:
            raise RuntimeError("no options chain snapshot from redis pubsub")
        ohlc = pd.DataFrame(list(self._ohlc_bars))
        ohlc["timestamp"] = pd.to_datetime(ohlc["timestamp"], errors="coerce")
        ohlc = ohlc.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        if len(ohlc) == 0:
            raise RuntimeError("invalid ohlc state")
        row = build_live_canonical_event(
            ohlc=ohlc,
            chain=dict(self._latest_chain),
            options_extractor=LiveMarketFeatureClient._extract_option_slice,
            rsi_fn=LiveMarketFeatureClient._rsi,
            atr_fn=LiveMarketFeatureClient._atr,
            vwap_fn=LiveMarketFeatureClient._vwap,
            vix_snapshot=_build_live_vix_snapshot(self._vix_daily, pd.Timestamp(ohlc.iloc[-1]["timestamp"]).date()),
        )
        self._refresh_depth_from_redis()
        depth = self._latest_depth or {}
        for key in (
            "depth_timestamp",
            "depth_total_bid_qty",
            "depth_total_ask_qty",
            "depth_top_bid_qty",
            "depth_top_ask_qty",
            "depth_top_bid_price",
            "depth_top_ask_price",
            "depth_spread",
            "depth_imbalance",
        ):
            row[key] = depth.get(key, float("nan"))
        (
            self._prev_trade_date,
            self._prev_opt0_ce_close,
            self._prev_opt0_pe_close,
            self._prev_opt0_total_oi,
        ) = _apply_live_option_change_features(
            row,
            prev_trade_date=self._prev_trade_date,
            prev_opt0_ce_close=self._prev_opt0_ce_close,
            prev_opt0_pe_close=self._prev_opt0_pe_close,
            prev_opt0_total_oi=self._prev_opt0_total_oi,
        )
        return row

    @property
    def depth_updates(self) -> int:
        return int(self._depth_updates)


def run_live_api_paper_loop(
    instrument: str,
    model_package: Dict[str, object],
    thresholds: DecisionThresholds,
    output_jsonl: Path,
    mode: str = "dual",
    market_api_base: str = "http://127.0.0.1:8004",
    dashboard_api_base: str = "http://127.0.0.1:8002",
    poll_seconds: float = 5.0,
    max_iterations: Optional[int] = None,
) -> Dict[str, object]:
    client = LiveMarketFeatureClient(
        market_api_base=market_api_base,
        dashboard_api_base=dashboard_api_base,
        timeout_seconds=max(2.0, poll_seconds),
    )
    decisions: List[Dict[str, object]] = []
    last_ts: Optional[str] = None
    iterations = 0
    while True:
        iterations += 1
        row = client.build_latest_feature_row(instrument=instrument)
        ts = str(row.get("timestamp"))
        if ts != last_ts:
            decision = predict_decision_from_row(
                row,
                model_package,
                thresholds,
                mode=mode,
                require_complete_row_inputs=True,
            )
            decision["source"] = "live_api"
            decision["instrument"] = instrument
            decisions.append(decision)
            _append_jsonl(output_jsonl, [decision])
            last_ts = ts
        if max_iterations is not None and iterations >= int(max_iterations):
            break
        time.sleep(max(0.1, float(poll_seconds)))

    counts: Dict[str, int] = {}
    for d in decisions:
        counts[d["action"]] = counts.get(d["action"], 0) + 1
    return {
        "mode": mode,
        "iterations": int(iterations),
        "decisions_emitted": int(len(decisions)),
        "action_counts": counts,
        "output_jsonl": str(output_jsonl),
    }


def run_live_api_paper_loop_v2(
    instrument: str,
    model_package: Dict[str, object],
    thresholds: DecisionThresholds,
    output_jsonl: Path,
    mode: str = "dual",
    market_api_base: str = "http://127.0.0.1:8004",
    dashboard_api_base: str = "http://127.0.0.1:8002",
    poll_seconds: float = 5.0,
    max_iterations: Optional[int] = None,
    max_hold_minutes: int = 5,
    confidence_buffer: float = 0.05,
    stagnation_enabled: bool = False,
    stagnation_window_minutes: int = 10,
    stagnation_threshold_pct: float = 0.008,
    stagnation_volatility_multiplier: float = 2.0,
    stagnation_min_hold_minutes: int = 0,
) -> Dict[str, object]:
    client = LiveMarketFeatureClient(
        market_api_base=market_api_base,
        dashboard_api_base=dashboard_api_base,
        timeout_seconds=max(2.0, poll_seconds),
    )
    events: List[Dict[str, object]] = []
    position: Optional[Dict[str, object]] = None
    last_ts: Optional[str] = None
    iterations = 0
    last_row: Optional[Dict[str, object]] = None
    while True:
        iterations += 1
        row = client.build_latest_feature_row(instrument=instrument)
        last_row = dict(row)
        ts = str(row.get("timestamp"))
        if ts != last_ts:
            decision = predict_decision_from_row(
                row,
                model_package,
                thresholds,
                mode=mode,
                require_complete_row_inputs=True,
            )
            decision["source"] = "live_api"
            decision["instrument"] = instrument
            event, position = _emit_exit_aware_event(
                decision=decision,
                position=position,
                thresholds=thresholds,
                max_hold_minutes=int(max_hold_minutes),
                confidence_buffer=float(confidence_buffer),
                current_ce_price=_safe_positive_price(row.get("opt_0_ce_close")),
                current_pe_price=_safe_positive_price(row.get("opt_0_pe_close")),
                stagnation_enabled=bool(stagnation_enabled),
                stagnation_window_minutes=int(stagnation_window_minutes),
                stagnation_threshold_pct=float(stagnation_threshold_pct),
                stagnation_volatility_multiplier=float(stagnation_volatility_multiplier),
                stagnation_min_hold_minutes=int(stagnation_min_hold_minutes),
            )
            event, position = _attach_event_context(
                event=event,
                position=position,
                row=row,
                instrument_hint=str(instrument),
            )
            events.append(event)
            _append_jsonl(output_jsonl, [event])
            last_ts = ts
        if max_iterations is not None and iterations >= int(max_iterations):
            break
        time.sleep(max(0.1, float(poll_seconds)))

    if position is not None and last_ts:
        session_end_event = _build_session_end_event(
            mode=mode,
            thresholds=thresholds,
            last_timestamp=pd.Timestamp(last_ts),
            position=position,
        )
        session_end_event["source"] = "live_api"
        session_end_event["instrument"] = instrument
        session_end_event, position = _attach_event_context(
            event=session_end_event,
            position=position,
            row=last_row,
            instrument_hint=str(instrument),
        )
        events.append(session_end_event)
        _append_jsonl(output_jsonl, [session_end_event])
        position = None

    event_counts: Dict[str, int] = {}
    event_reason_counts: Dict[str, int] = {}
    for e in events:
        key = str(e.get("event_type"))
        event_counts[key] = event_counts.get(key, 0) + 1
        reason = str(e.get("event_reason"))
        event_reason_counts[reason] = event_reason_counts.get(reason, 0) + 1
    return {
        "mode": mode,
        "iterations": int(iterations),
        "events_emitted": int(len(events)),
        "event_counts": event_counts,
        "event_reason_counts": event_reason_counts,
        "stagnation_exit": {
            "enabled": bool(stagnation_enabled),
            "window_minutes": int(stagnation_window_minutes),
            "threshold_pct": float(stagnation_threshold_pct),
            "volatility_multiplier": float(stagnation_volatility_multiplier),
            "min_hold_minutes": int(stagnation_min_hold_minutes),
        },
        "output_jsonl": str(output_jsonl),
    }


def run_live_redis_event_loop_v2(
    instrument: str,
    model_package: Dict[str, object],
    thresholds: DecisionThresholds,
    output_jsonl: Path,
    mode: str = "dual",
    redis_host: str = "localhost",
    redis_port: int = 6379,
    redis_db: int = 0,
    redis_password: Optional[str] = None,
    redis_timeout_seconds: float = 2.0,
    ohlc_pattern: Optional[str] = None,
    options_channel: Optional[str] = None,
    depth_channel: Optional[str] = None,
    max_iterations: Optional[int] = None,
    max_hold_minutes: int = 5,
    confidence_buffer: float = 0.05,
    max_idle_seconds: float = 90.0,
    stagnation_enabled: bool = False,
    stagnation_window_minutes: int = 10,
    stagnation_threshold_pct: float = 0.008,
    stagnation_volatility_multiplier: float = 2.0,
    stagnation_min_hold_minutes: int = 0,
) -> Dict[str, object]:
    ohlc_subscription = str(ohlc_pattern or f"market:ohlc:{instrument}:*")
    options_subscription = str(options_channel or f"market:options:{instrument}")
    depth_subscription = str(depth_channel or f"market:depth:{instrument}")
    options_alt = f"market:options:{str(instrument).upper()}"
    depth_alt = f"market:depth:{str(instrument).upper()}"
    conn_kwargs: Dict[str, object] = {
        "host": str(redis_host),
        "port": int(redis_port),
        "db": int(redis_db),
        "decode_responses": True,
        "socket_connect_timeout": float(redis_timeout_seconds),
        "socket_timeout": float(redis_timeout_seconds),
    }
    if redis_password:
        conn_kwargs["password"] = str(redis_password)
    client = redis.Redis(**conn_kwargs)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.psubscribe(ohlc_subscription)
    pubsub.subscribe(options_subscription)
    if options_alt != options_subscription:
        pubsub.subscribe(options_alt)
    pubsub.subscribe(depth_subscription)
    if depth_alt != depth_subscription:
        pubsub.subscribe(depth_alt)

    feature_client = RedisEventFeatureClient(
        instrument=instrument,
        max_bars=120,
        redis_client=client,
        mode_hint=str(mode or "").strip().lower(),
    )
    events: List[Dict[str, object]] = []
    position: Optional[Dict[str, object]] = None
    last_ts: Optional[str] = None
    processed = 0
    messages_total = 0
    no_feature_count = 0
    idle_start = time.monotonic()
    last_row: Optional[Dict[str, object]] = None
    try:
        while True:
            msg = pubsub.get_message(timeout=max(0.1, float(redis_timeout_seconds)))
            if msg is None:
                if max_idle_seconds is not None and (time.monotonic() - idle_start) >= float(max_idle_seconds):
                    break
                continue

            idle_start = time.monotonic()
            messages_total += 1
            emitted_ts = feature_client.consume_redis_message(msg)
            if emitted_ts is None or emitted_ts == last_ts:
                continue

            try:
                row = feature_client.build_latest_feature_row()
            except Exception:
                no_feature_count += 1
                continue
            last_row = dict(row)
            decision = predict_decision_from_row(
                row,
                model_package,
                thresholds,
                mode=mode,
                require_complete_row_inputs=True,
            )
            decision["source"] = "redis_pubsub"
            decision["instrument"] = instrument
            decision["depth"] = {
                "timestamp": row.get("depth_timestamp"),
                "total_bid_qty": row.get("depth_total_bid_qty"),
                "total_ask_qty": row.get("depth_total_ask_qty"),
                "top_bid_qty": row.get("depth_top_bid_qty"),
                "top_ask_qty": row.get("depth_top_ask_qty"),
                "top_bid_price": row.get("depth_top_bid_price"),
                "top_ask_price": row.get("depth_top_ask_price"),
                "spread": row.get("depth_spread"),
                "imbalance": row.get("depth_imbalance"),
            }
            event, position = _emit_exit_aware_event(
                decision=decision,
                position=position,
                thresholds=thresholds,
                max_hold_minutes=int(max_hold_minutes),
                confidence_buffer=float(confidence_buffer),
                current_ce_price=_safe_positive_price(row.get("opt_0_ce_close")),
                current_pe_price=_safe_positive_price(row.get("opt_0_pe_close")),
                stagnation_enabled=bool(stagnation_enabled),
                stagnation_window_minutes=int(stagnation_window_minutes),
                stagnation_threshold_pct=float(stagnation_threshold_pct),
                stagnation_volatility_multiplier=float(stagnation_volatility_multiplier),
                stagnation_min_hold_minutes=int(stagnation_min_hold_minutes),
            )
            event, position = _attach_event_context(
                event=event,
                position=position,
                row=row,
                instrument_hint=str(instrument),
            )
            events.append(event)
            _append_jsonl(output_jsonl, [event])
            last_ts = emitted_ts
            processed += 1
            if max_iterations is not None and processed >= int(max_iterations):
                break
    finally:
        try:
            pubsub.close()
        except Exception:
            pass

    if position is not None and last_ts:
        session_end_event = _build_session_end_event(
            mode=mode,
            thresholds=thresholds,
            last_timestamp=pd.Timestamp(last_ts),
            position=position,
        )
        session_end_event["source"] = "redis_pubsub"
        session_end_event["instrument"] = instrument
        session_end_event, position = _attach_event_context(
            event=session_end_event,
            position=position,
            row=last_row,
            instrument_hint=str(instrument),
        )
        events.append(session_end_event)
        _append_jsonl(output_jsonl, [session_end_event])
        position = None

    event_counts: Dict[str, int] = {}
    event_reason_counts: Dict[str, int] = {}
    for e in events:
        key = str(e.get("event_type"))
        event_counts[key] = event_counts.get(key, 0) + 1
        reason = str(e.get("event_reason"))
        event_reason_counts[reason] = event_reason_counts.get(reason, 0) + 1
    return {
        "mode": mode,
        "messages_total": int(messages_total),
        "bars_processed": int(processed),
        "events_emitted": int(len(events)),
        "event_counts": event_counts,
        "event_reason_counts": event_reason_counts,
        "no_feature_rows": int(no_feature_count),
        "subscriptions": {
            "ohlc_pattern": ohlc_subscription,
            "options_channel": options_subscription,
            "depth_channel": depth_subscription,
        },
        "stagnation_exit": {
            "enabled": bool(stagnation_enabled),
            "window_minutes": int(stagnation_window_minutes),
            "threshold_pct": float(stagnation_threshold_pct),
            "volatility_multiplier": float(stagnation_volatility_multiplier),
            "min_hold_minutes": int(stagnation_min_hold_minutes),
        },
        "depth_updates": int(feature_client.depth_updates),
        "output_jsonl": str(output_jsonl),
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Live inference adapter (paper mode)")
    parser.add_argument("--model-package", default="ml_pipeline/artifacts/t06_baseline_model.joblib")
    parser.add_argument("--threshold-report", default="ml_pipeline/artifacts/t08_threshold_report.json")
    parser.add_argument("--mode", default="dual", choices=["dual", "ce_only", "pe_only"])
    parser.add_argument(
        "--run-mode",
        default="replay-dry-run",
        choices=["replay-dry-run", "replay-dry-run-v2", "live-api", "live-api-v2", "live-redis-v2"],
    )
    parser.add_argument("--feature-parquet", default="ml_pipeline/artifacts/t04_features.parquet")
    parser.add_argument("--output-jsonl", default="ml_pipeline/artifacts/t11_paper_decisions.jsonl")
    parser.add_argument("--limit", type=int, default=200, help="Replay row limit for dry-run mode")
    parser.add_argument("--instrument", default="BANKNIFTY-I", help="Instrument symbol for live-api mode")
    parser.add_argument("--market-api-base", default="http://127.0.0.1:8004")
    parser.add_argument("--dashboard-api-base", default="http://127.0.0.1:8002")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-iterations", type=int, default=60)
    parser.add_argument("--max-hold-minutes", type=int, default=5, help="Exit-aware replay mode max hold minutes")
    parser.add_argument("--confidence-buffer", type=float, default=0.05, help="Exit-aware replay confidence fade buffer")
    parser.add_argument("--stagnation-enabled", action="store_true", help="Enable low-movement stagnation exit overlay")
    parser.add_argument("--stagnation-window-minutes", type=int, default=10, help="Bars/minutes window for stagnation check")
    parser.add_argument(
        "--stagnation-threshold-pct",
        type=float,
        default=0.8,
        help="Base stagnation threshold percentage of entry price (e.g. 0.8 = 0.8%%)",
    )
    parser.add_argument(
        "--stagnation-volatility-multiplier",
        type=float,
        default=2.0,
        help="Adaptive multiplier applied to median step pct within stagnation window",
    )
    parser.add_argument(
        "--stagnation-min-hold-minutes",
        type=int,
        default=0,
        help="Minimum hold before stagnation exits are allowed",
    )
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "localhost"))
    parser.add_argument("--redis-port", type=int, default=int(os.getenv("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.getenv("REDIS_DB", "0")))
    parser.add_argument("--redis-password", default=os.getenv("REDIS_PASSWORD"))
    parser.add_argument("--redis-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--ohlc-pattern", default=None, help="Redis pattern for OHLC pubsub channel")
    parser.add_argument("--options-channel", default=None, help="Redis channel for options chain pubsub")
    parser.add_argument("--depth-channel", default=None, help="Redis channel for depth pubsub")
    parser.add_argument("--max-idle-seconds", type=float, default=90.0)
    args = parser.parse_args(list(argv) if argv is not None else None)

    model_path = Path(args.model_package)
    threshold_path = Path(args.threshold_report)
    out_path = Path(args.output_jsonl)
    if not model_path.exists():
        print(f"ERROR: model package not found: {model_path}")
        return 2
    if not threshold_path.exists():
        print(f"ERROR: threshold report not found: {threshold_path}")
        return 2

    model_package = load_model_package(model_path)
    thresholds = load_thresholds(threshold_path)

    if args.run_mode == "replay-dry-run":
        feature_path = Path(args.feature_parquet)
        if not feature_path.exists():
            print(f"ERROR: feature parquet not found: {feature_path}")
            return 2
        summary = run_replay_dry_run(
            feature_parquet=feature_path,
            model_package=model_package,
            thresholds=thresholds,
            output_jsonl=out_path,
            mode=args.mode,
            limit=args.limit,
        )
    elif args.run_mode == "replay-dry-run-v2":
        feature_path = Path(args.feature_parquet)
        if not feature_path.exists():
            print(f"ERROR: feature parquet not found: {feature_path}")
            return 2
        summary = run_replay_dry_run_v2(
            feature_parquet=feature_path,
            model_package=model_package,
            thresholds=thresholds,
            output_jsonl=out_path,
            mode=args.mode,
            limit=args.limit,
            max_hold_minutes=int(args.max_hold_minutes),
            confidence_buffer=float(args.confidence_buffer),
            stagnation_enabled=bool(args.stagnation_enabled),
            stagnation_window_minutes=int(max(2, args.stagnation_window_minutes)),
            stagnation_threshold_pct=float(max(0.0, args.stagnation_threshold_pct) / 100.0),
            stagnation_volatility_multiplier=float(max(0.0, args.stagnation_volatility_multiplier)),
            stagnation_min_hold_minutes=int(max(0, args.stagnation_min_hold_minutes)),
        )
    elif args.run_mode == "live-api-v2":
        summary = run_live_api_paper_loop_v2(
            instrument=args.instrument,
            model_package=model_package,
            thresholds=thresholds,
            output_jsonl=out_path,
            mode=args.mode,
            market_api_base=args.market_api_base,
            dashboard_api_base=args.dashboard_api_base,
            poll_seconds=args.poll_seconds,
            max_iterations=args.max_iterations,
            max_hold_minutes=int(args.max_hold_minutes),
            confidence_buffer=float(args.confidence_buffer),
            stagnation_enabled=bool(args.stagnation_enabled),
            stagnation_window_minutes=int(max(2, args.stagnation_window_minutes)),
            stagnation_threshold_pct=float(max(0.0, args.stagnation_threshold_pct) / 100.0),
            stagnation_volatility_multiplier=float(max(0.0, args.stagnation_volatility_multiplier)),
            stagnation_min_hold_minutes=int(max(0, args.stagnation_min_hold_minutes)),
        )
    elif args.run_mode == "live-redis-v2":
        summary = run_live_redis_event_loop_v2(
            instrument=args.instrument,
            model_package=model_package,
            thresholds=thresholds,
            output_jsonl=out_path,
            mode=args.mode,
            redis_host=args.redis_host,
            redis_port=int(args.redis_port),
            redis_db=int(args.redis_db),
            redis_password=args.redis_password,
            redis_timeout_seconds=float(args.redis_timeout_seconds),
            ohlc_pattern=args.ohlc_pattern,
            options_channel=args.options_channel,
            depth_channel=args.depth_channel,
            max_iterations=args.max_iterations,
            max_hold_minutes=int(args.max_hold_minutes),
            confidence_buffer=float(args.confidence_buffer),
            max_idle_seconds=float(args.max_idle_seconds),
            stagnation_enabled=bool(args.stagnation_enabled),
            stagnation_window_minutes=int(max(2, args.stagnation_window_minutes)),
            stagnation_threshold_pct=float(max(0.0, args.stagnation_threshold_pct) / 100.0),
            stagnation_volatility_multiplier=float(max(0.0, args.stagnation_volatility_multiplier)),
            stagnation_min_hold_minutes=int(max(0, args.stagnation_min_hold_minutes)),
        )
    else:
        summary = run_live_api_paper_loop(
            instrument=args.instrument,
            model_package=model_package,
            thresholds=thresholds,
            output_jsonl=out_path,
            mode=args.mode,
            market_api_base=args.market_api_base,
            dashboard_api_base=args.dashboard_api_base,
            poll_seconds=args.poll_seconds,
            max_iterations=args.max_iterations,
        )

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
