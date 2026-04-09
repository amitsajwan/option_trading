from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import joblib
import numpy as np
import pandas as pd


def load_model_package(path: Path) -> Dict[str, object]:
    payload = joblib.load(path)
    if not isinstance(payload, dict):
        raise ValueError(f"model package must be a dict: {path}")
    return payload


def validate_model_input_columns(available_columns: Sequence[str], model_package: Dict[str, object], *, context: str, missing_policy_override: Optional[str] = None) -> Dict[str, object]:
    contract = model_package.get("_model_input_contract") if isinstance(model_package, dict) else None
    if not isinstance(contract, dict):
        contract = {"required_features": list(model_package.get("feature_columns") or []), "missing_policy": "error", "source": "feature_columns"}
    required = [str(name) for name in list(contract.get("required_features") or [])]
    available = {str(name) for name in available_columns}
    missing_required = [name for name in required if name not in available]
    policy = str(missing_policy_override or contract.get("missing_policy", "error")).strip().lower() or "error"
    if policy == "error" and missing_required:
        raise ValueError(f"{context}: missing required model input fields ({len(missing_required)}): {', '.join(missing_required[:30])}")
    return {
        "required_count": int(len(required)),
        "missing_required_count": int(len(missing_required)),
        "missing_required_features": list(missing_required),
        "missing_policy": policy,
        "contract_source": str(contract.get("source", "unknown")),
    }


def _predict_proba(model: object, x: pd.DataFrame) -> np.ndarray:
    out = model.predict_proba(x)
    return np.asarray(out, dtype=float)


def _empty_probability_frame(model_package: Dict[str, object]) -> pd.DataFrame:
    prediction_mode = str(model_package.get("prediction_mode") or "").strip().lower()
    if prediction_mode == "direction_or_no_trade":
        return pd.DataFrame(
            {
                "direction_trade_prob": pd.Series(dtype=float),
                "direction_up_prob": pd.Series(dtype=float),
                "ce_prob": pd.Series(dtype=float),
                "pe_prob": pd.Series(dtype=float),
            }
        )
    models = dict(model_package.get("models") or {})
    single_target = model_package.get("single_target") if isinstance(model_package, dict) else None
    if isinstance(single_target, dict):
        model_key = str(single_target.get("model_key", "")).strip()
        prob_col = str(single_target.get("prob_col", "")).strip()
        if model_key and prob_col and model_key in models and "ce" not in models and "pe" not in models:
            return pd.DataFrame({prob_col: pd.Series(dtype=float)})
    if "move" in models and "ce" not in models and "pe" not in models:
        return pd.DataFrame({"move_prob": pd.Series(dtype=float)})
    if "direction" in models and "ce" not in models and "pe" not in models:
        return pd.DataFrame({"direction_up_prob": pd.Series(dtype=float)})
    return pd.DataFrame({"ce_prob": pd.Series(dtype=float), "pe_prob": pd.Series(dtype=float)})


def predict_probabilities_from_frame(feature_df: pd.DataFrame, model_package: Dict[str, object], *, missing_policy_override: Optional[str] = None, context: str = "predict_probabilities_from_frame") -> Tuple[pd.DataFrame, Dict[str, object]]:
    prediction_mode = str(model_package.get("prediction_mode") or "").strip().lower()
    if prediction_mode == "direction_or_no_trade":
        trade_gate_package = model_package.get("trade_gate_package")
        direction_package = model_package.get("direction_package")
        if not isinstance(trade_gate_package, dict) or not isinstance(direction_package, dict):
            raise ValueError("direction_or_no_trade package requires trade_gate_package and direction_package")
        trade_probs, trade_validation = predict_probabilities_from_frame(
            feature_df,
            trade_gate_package,
            missing_policy_override=missing_policy_override,
            context=f"{context}.trade_gate",
        )
        direction_probs, _ = predict_probabilities_from_frame(
            feature_df,
            direction_package,
            missing_policy_override=missing_policy_override,
            context=f"{context}.direction",
        )
        trade_col = "move_prob" if "move_prob" in trade_probs.columns else str(trade_probs.columns[0])
        direction_col = "direction_up_prob" if "direction_up_prob" in direction_probs.columns else str(direction_probs.columns[0])
        direction_trade_prob = pd.to_numeric(trade_probs[trade_col], errors="coerce")
        direction_up_prob = pd.to_numeric(direction_probs[direction_col], errors="coerce")
        return (
            pd.DataFrame(
                {
                    "direction_trade_prob": direction_trade_prob,
                    "direction_up_prob": direction_up_prob,
                    "ce_prob": direction_trade_prob * direction_up_prob,
                    "pe_prob": direction_trade_prob * (1.0 - direction_up_prob),
                }
            ),
            trade_validation,
        )
    validation = validate_model_input_columns(list(feature_df.columns), model_package, context=context, missing_policy_override=missing_policy_override)
    feature_cols = [str(col) for col in list(model_package["feature_columns"])]
    x = feature_df.copy()
    for col in feature_cols:
        if col not in x.columns:
            x[col] = np.nan
    x = x.loc[:, feature_cols]
    if len(x) == 0:
        return _empty_probability_frame(model_package), validation
    models = dict(model_package["models"])
    single_target = model_package.get("single_target") if isinstance(model_package, dict) else None
    if isinstance(single_target, dict):
        model_key = str(single_target.get("model_key", "")).strip()
        prob_col = str(single_target.get("prob_col", "")).strip()
        if model_key and prob_col and model_key in models and "ce" not in models and "pe" not in models:
            prob = _predict_proba(models[model_key], x)[:, 1]
            return pd.DataFrame({prob_col: prob}), validation
    if "move" in models and "ce" not in models and "pe" not in models:
        move_prob = _predict_proba(models["move"], x)[:, 1]
        return pd.DataFrame({"move_prob": move_prob}), validation
    if "direction" in models and "ce" not in models and "pe" not in models:
        direction_prob = _predict_proba(models["direction"], x)[:, 1]
        return pd.DataFrame({"direction_up_prob": direction_prob}), validation
    ce_prob = _predict_proba(models["ce"], x)[:, 1]
    pe_prob = _predict_proba(models["pe"], x)[:, 1]
    return pd.DataFrame({"ce_prob": ce_prob, "pe_prob": pe_prob}), validation
