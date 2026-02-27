from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import warnings

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

from .calibration import CALIBRATION_ISOTONIC, calibrate_probs
from .metrics import classification_metrics, threshold_stats
from .thresholds import (
    RANKING_MODE_LEGACY,
    choose_threshold,
    choose_threshold_walk_forward,
    threshold_grid,
)


LABEL_TARGET_BASE = "base_label"
LABEL_TARGET_FORWARD_RETURN_THRESHOLD = "forward_return_threshold"
LABEL_TARGET_PATH_TP_SL = "path_tp_sl"
LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO = "path_tp_sl_time_stop_zero"


@dataclass(frozen=True)
class LightGBMConfig:
    n_estimators: int = 400
    learning_rate: float = 0.03
    num_leaves: int = 63
    max_depth: int = -1
    subsample: float = 0.9
    colsample_bytree: float = 0.9
    random_state: int = 42
    auto_scale_pos_weight: bool = True
    min_scale_pos_weight: float = 1.0
    max_scale_pos_weight: float = 50.0


@dataclass(frozen=True)
class ThresholdPolicy:
    min_value: float = 0.30
    max_value: float = 0.90
    step: float = 0.01
    cost_per_trade: float = 0.0006
    min_profit_factor: float = 1.3
    max_drawdown_pct: float = 0.15
    min_trades: int = 50
    min_pos_rate: float = 0.01
    max_pos_rate: float = 0.20
    strict_pos_rate_guard: bool = False
    selection_mode: str = "single"
    walk_forward_folds: int = 4
    min_fold_pass_ratio: float = 0.75
    ranking_mode: str = RANKING_MODE_LEGACY


def _build_lgbm_pipeline(cfg: LightGBMConfig, *, scale_pos_weight: float) -> Pipeline:
    try:
        from lightgbm import LGBMClassifier
    except Exception as exc:
        raise RuntimeError("LightGBM is required. Install it first: pip install lightgbm") from exc
    model = LGBMClassifier(
        objective="binary",
        n_estimators=int(cfg.n_estimators),
        learning_rate=float(cfg.learning_rate),
        num_leaves=int(cfg.num_leaves),
        max_depth=int(cfg.max_depth),
        subsample=float(cfg.subsample),
        colsample_bytree=float(cfg.colsample_bytree),
        random_state=int(cfg.random_state),
        n_jobs=1,
        scale_pos_weight=float(scale_pos_weight),
    )
    imputer = SimpleImputer(strategy="median", add_indicator=True)
    # Preserve DataFrame column names through the imputer so LightGBM receives
    # named features at predict time. Without this, sklearn warns that
    # "X does not have valid feature names" and positional column order becomes
    # the only guarantee — silent wrong predictions if columns are ever reordered.
    try:
        imputer.set_output(transform="pandas")
    except AttributeError:
        pass  # sklearn < 1.2 — no set_output, warning is cosmetic only
    return Pipeline(
        steps=[
            ("imputer", imputer),
            ("model", model),
        ]
    )


def _feature_importance(model: Pipeline, feature_columns: list[str], top_k: int = 30) -> List[Dict[str, float]]:
    inner = model.named_steps["model"]
    values = getattr(inner, "feature_importances_", None)
    if values is None:
        return []
    imputer = model.named_steps.get("imputer")
    if imputer is not None and hasattr(imputer, "get_feature_names_out"):
        try:
            feature_names = list(imputer.get_feature_names_out(feature_columns))
        except Exception:
            feature_names = list(feature_columns)
    else:
        feature_names = list(feature_columns)
    out: List[Dict[str, float]] = []
    for name, score in zip(feature_names, values):
        out.append({"feature": str(name), "importance": float(score)})
    out.sort(key=lambda row: row["importance"], reverse=True)
    return out[: int(top_k)]


def _build_xy_for_label_target(
    frame: pd.DataFrame,
    *,
    side: str,
    feature_columns: list[str],
    label_target: str,
    min_move_pct: Optional[float],
    label_horizon_minutes: Optional[int],
) -> Tuple[pd.DataFrame, np.ndarray]:
    prefix = str(side).lower()
    suffix = f"_h{int(label_horizon_minutes)}m" if label_horizon_minutes is not None else ""
    valid_col = f"{prefix}_label_valid{suffix}"
    label_col = f"{prefix}_label{suffix}"
    ret_col = f"{prefix}_forward_return{suffix}"
    mode = str(label_target).strip().lower()
    if mode == LABEL_TARGET_BASE:
        if label_col not in frame.columns or valid_col not in frame.columns:
            raise ValueError(f"missing columns for base label target: {label_col}/{valid_col}")
        work = frame[(pd.to_numeric(frame[valid_col], errors="coerce") == 1.0) & frame[label_col].notna()].copy()
        if len(work) == 0:
            raise ValueError(f"empty training rows for {label_col}")
        x = work.loc[:, list(feature_columns)].copy()
        y = pd.to_numeric(work[label_col], errors="coerce").fillna(0.0).astype(int).to_numpy()
        return x, y

    if mode == LABEL_TARGET_FORWARD_RETURN_THRESHOLD:
        if ret_col not in frame.columns or valid_col not in frame.columns:
            raise ValueError(f"missing columns for forward-return label target: {ret_col}/{valid_col}")
        work = frame[(pd.to_numeric(frame[valid_col], errors="coerce") == 1.0) & frame[ret_col].notna()].copy()
        if len(work) == 0:
            raise ValueError(f"empty training rows for {ret_col}")
        threshold = float(min_move_pct) if min_move_pct is not None else 0.0
        ret = pd.to_numeric(work[ret_col], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        x = work.loc[:, list(feature_columns)].copy()
        y = (ret >= threshold).astype(int)
        return x, y

    if mode in {LABEL_TARGET_PATH_TP_SL, LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO}:
        reason_col = f"{prefix}_path_exit_reason"
        if reason_col not in frame.columns or valid_col not in frame.columns:
            raise ValueError(f"missing columns for path label target: {reason_col}/{valid_col}")
        work = frame[pd.to_numeric(frame[valid_col], errors="coerce") == 1.0].copy()
        reasons = work[reason_col].astype(str).str.strip().str.lower()
        y_raw = np.where(
            reasons.isin(["tp", "tp_sl_same_bar"]),
            1.0,
            np.where(reasons.eq("sl"), 0.0, np.nan),
        )
        if mode == LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO:
            y_raw = np.where(np.isnan(y_raw) & reasons.eq("time_stop"), 0.0, y_raw)
        work["target"] = y_raw
        work = work[work["target"].notna()].copy()
        if len(work) == 0:
            raise ValueError(f"empty training rows for {reason_col} under mode={mode}")
        x = work.loc[:, list(feature_columns)].copy()
        y = work["target"].astype(int).to_numpy()
        return x, y

    raise ValueError(f"unsupported label_target: {label_target}")


def train_side(
    *,
    side: str,
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    eval_df: pd.DataFrame,
    feature_columns: list[str],
    config: LightGBMConfig,
    threshold_policy: ThresholdPolicy,
    calibration_method: str = CALIBRATION_ISOTONIC,
    label_target: str = LABEL_TARGET_BASE,
    min_move_pct: Optional[float] = None,
    label_horizon_minutes: Optional[int] = None,
) -> Tuple[Pipeline, Dict[str, object]]:
    side = str(side).lower()
    suffix = f"_h{int(label_horizon_minutes)}m" if label_horizon_minutes is not None else ""
    label_col = f"{side}_label{suffix}"
    ret_col = f"{side}_forward_return{suffix}"
    x_train, y_train = _build_xy_for_label_target(
        train_df,
        side=side,
        feature_columns=feature_columns,
        label_target=label_target,
        min_move_pct=min_move_pct,
        label_horizon_minutes=label_horizon_minutes,
    )
    x_valid, y_valid = _build_xy_for_label_target(
        valid_df,
        side=side,
        feature_columns=feature_columns,
        label_target=label_target,
        min_move_pct=min_move_pct,
        label_horizon_minutes=label_horizon_minutes,
    )
    x_eval, y_eval = _build_xy_for_label_target(
        eval_df,
        side=side,
        feature_columns=feature_columns,
        label_target=label_target,
        min_move_pct=min_move_pct,
        label_horizon_minutes=label_horizon_minutes,
    )
    train_pos_rate = float(np.mean(y_train)) if len(y_train) else 0.0
    valid_pos_rate = float(np.mean(y_valid)) if len(y_valid) else 0.0
    min_pos_rate = float(threshold_policy.min_pos_rate)
    max_pos_rate = float(threshold_policy.max_pos_rate)
    for split_name, y in (("train", y_train), ("valid", y_valid), ("eval", y_eval)):
        unique = np.unique(y)
        if len(unique) < 2:
            counts = {int(k): int(np.sum(y == k)) for k in unique}
            raise ValueError(
                f"{side.upper()} label target has single class on {split_name}: {counts}. "
                f"label_target={label_target}, min_move_pct={min_move_pct}. "
                "Relax move threshold or switch label target."
            )
    pos_rate_warnings: List[str] = []
    for split_name, pos_rate in (("train", train_pos_rate), ("valid", valid_pos_rate)):
        if pos_rate < min_pos_rate or pos_rate > max_pos_rate:
            msg = (
                f"{side.upper()} label positive-rate guard failed on {split_name}: "
                f"pos_rate={pos_rate:.6f}, required range=[{min_pos_rate:.6f}, {max_pos_rate:.6f}]. "
                f"label_target={label_target}, min_move_pct={min_move_pct}. "
                "Adjust label target / move threshold / positive-rate guard."
            )
            if bool(threshold_policy.strict_pos_rate_guard):
                raise ValueError(msg)
            warnings.warn(msg, RuntimeWarning)
            pos_rate_warnings.append(msg)

    pos_count = float(np.sum(y_train == 1))
    neg_count = float(np.sum(y_train == 0))
    scale_pos_weight = 1.0
    if bool(config.auto_scale_pos_weight) and pos_count > 0.0:
        raw_ratio = neg_count / pos_count
        scale_pos_weight = float(np.clip(raw_ratio, config.min_scale_pos_weight, config.max_scale_pos_weight))

    model = _build_lgbm_pipeline(config, scale_pos_weight=scale_pos_weight)
    model.fit(x_train, y_train)
    p_valid_raw = model.predict_proba(x_valid)[:, 1]
    p_eval_raw = model.predict_proba(x_eval)[:, 1]
    p_valid, p_eval = calibrate_probs(
        method=calibration_method,
        valid_prob=p_valid_raw,
        valid_label=y_valid,
        test_prob=p_eval_raw,
    )

    grid = threshold_grid(threshold_policy.min_value, threshold_policy.max_value, threshold_policy.step)
    # Drop rows where ret_col is NaN (end-of-session bars, data gaps) rather than
    # imputing 0.0 — a NaN return is not a flat trade, it's unusable data.
    # We align prob arrays to match so threshold_stats remains length-consistent.
    _ret_valid_raw = pd.to_numeric(valid_df.loc[x_valid.index, ret_col], errors="coerce")
    _ret_valid_mask = _ret_valid_raw.notna().to_numpy()
    p_valid = p_valid[_ret_valid_mask]
    y_valid = y_valid[_ret_valid_mask]
    ret_valid = _ret_valid_raw[_ret_valid_mask].to_numpy()

    _ret_eval_raw = pd.to_numeric(eval_df.loc[x_eval.index, ret_col], errors="coerce")
    _ret_eval_mask = _ret_eval_raw.notna().to_numpy()
    p_eval = p_eval[_ret_eval_mask]
    y_eval = y_eval[_ret_eval_mask]
    ret_eval = _ret_eval_raw[_ret_eval_mask].to_numpy()
    selection_mode = str(threshold_policy.selection_mode).lower()
    try:
        if selection_mode == "walk_forward":
            day_values = pd.to_datetime(valid_df.loc[x_valid.index, "trade_date"], errors="coerce").dt.date.to_numpy()
            thr_report = choose_threshold_walk_forward(
                prob_valid=p_valid,
                ret_valid=ret_valid,
                day_values=day_values,
                grid=grid,
                cost_per_trade=float(threshold_policy.cost_per_trade),
                min_profit_factor=float(threshold_policy.min_profit_factor),
                max_drawdown_pct=float(threshold_policy.max_drawdown_pct),
                min_trades=int(threshold_policy.min_trades),
                folds=int(threshold_policy.walk_forward_folds),
                min_fold_pass_ratio=float(threshold_policy.min_fold_pass_ratio),
                ranking_mode=str(threshold_policy.ranking_mode),
            )
        else:
            thr_report = choose_threshold(
                prob_valid=p_valid,
                ret_valid=ret_valid,
                grid=grid,
                cost_per_trade=float(threshold_policy.cost_per_trade),
                min_profit_factor=float(threshold_policy.min_profit_factor),
                max_drawdown_pct=float(threshold_policy.max_drawdown_pct),
                min_trades=int(threshold_policy.min_trades),
                ranking_mode=str(threshold_policy.ranking_mode),
            )
            thr_report["selection_mode"] = "single"
    except ValueError as exc:
        raise ValueError(f"{side.upper()} threshold selection failed: {exc}") from exc
    chosen = float(thr_report["selected_threshold"])
    valid_thr_stats = threshold_stats(p_valid, ret_valid, chosen, threshold_policy.cost_per_trade)
    eval_thr_stats = threshold_stats(p_eval, ret_eval, chosen, threshold_policy.cost_per_trade)
    metrics = {
        "valid": classification_metrics(y_valid, p_valid),
        "eval": classification_metrics(y_eval, p_eval),
        "valid_threshold": valid_thr_stats,
        "eval_threshold": eval_thr_stats,
    }
    report: Dict[str, object] = {
        "side": side,
        "label_col": label_col,
        "label_target": str(label_target),
        "label_min_move_pct": (float(min_move_pct) if min_move_pct is not None else None),
        "label_horizon_minutes": (int(label_horizon_minutes) if label_horizon_minutes is not None else None),
        "calibration_method": calibration_method,
        "threshold": thr_report,
        "metrics": metrics,
        "rows": {
            "train": int(len(x_train)),
            "valid": int(len(x_valid)),
            "eval": int(len(x_eval)),
        },
        "class_balance": {
            "train_positive_rows": int(pos_count),
            "train_negative_rows": int(neg_count),
            "scale_pos_weight": float(scale_pos_weight),
        },
        "positive_rate_guard": {
            "strict": bool(threshold_policy.strict_pos_rate_guard),
            "warnings": pos_rate_warnings,
        },
        "feature_importance_top": _feature_importance(model, feature_columns),
        "probability_ranges": {
            "valid_min": float(np.min(p_valid)) if len(p_valid) else 0.0,
            "valid_max": float(np.max(p_valid)) if len(p_valid) else 0.0,
            "eval_min": float(np.min(p_eval)) if len(p_eval) else 0.0,
            "eval_max": float(np.max(p_eval)) if len(p_eval) else 0.0,
        },
    }
    return model, report
