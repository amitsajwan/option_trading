from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
try:
    from lightgbm import LGBMClassifier
except Exception:  # pragma: no cover
    LGBMClassifier = None  # type: ignore[assignment]
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, average_precision_score, brier_score_loss, f1_score, precision_score, recall_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from ..catalog.feature_sets import DEFAULT_FEATURE_SET_SPECS, feature_set_specs_by_name
from ..catalog.models import DEFAULT_MODEL_SPECS, model_specs_by_name
from ..contracts.types import (
    LABEL_TARGET_BASE,
    LABEL_TARGET_CHOICES,
    LABEL_TARGET_MOVE_BARRIER_HIT,
    LABEL_TARGET_MOVE_DIRECTION_UP,
    LABEL_TARGET_PATH_TP_SL,
    LABEL_TARGET_PATH_TP_SL_RESOLVED_ONLY,
    LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO,
    ModelSpec,
    PreprocessConfig,
    TradingObjectiveConfig,
)
from .event_purge import PURGE_MODE_DAYS, PURGE_MODE_EVENT_OVERLAP, apply_event_overlap_purge, infer_side_event_end_col, normalize_purge_mode
from .features import select_feature_columns
from .metrics import profit_factor
from .walk_forward import build_day_folds


class ConstantProbModel:
    def __init__(self, p1: float):
        self.p1 = float(p1)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        p1 = np.full(int(len(x)), self.p1, dtype=float)
        return np.column_stack([1.0 - p1, p1])


class QuantileClipper(BaseEstimator, TransformerMixin):
    def __init__(self, lower_q: float = 0.01, upper_q: float = 0.99):
        self.lower_q = float(lower_q)
        self.upper_q = float(upper_q)
        self.columns_: List[str] = []
        self.lower_bounds_: Dict[str, float] = {}
        self.upper_bounds_: Dict[str, float] = {}

    def fit(self, x: pd.DataFrame, y: Optional[np.ndarray] = None) -> "QuantileClipper":
        frame = pd.DataFrame(x).copy()
        self.columns_ = [str(col) for col in frame.columns]
        for col in self.columns_:
            series = pd.to_numeric(frame[col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            self.lower_bounds_[col] = float(series.quantile(self.lower_q)) if len(series) else float("nan")
            self.upper_bounds_[col] = float(series.quantile(self.upper_q)) if len(series) else float("nan")
        return self

    def transform(self, x: pd.DataFrame) -> pd.DataFrame:
        frame = pd.DataFrame(x).copy()
        for col in self.columns_:
            if col not in frame.columns:
                continue
            series = pd.to_numeric(frame[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
            lo = self.lower_bounds_.get(col, float("nan"))
            hi = self.upper_bounds_.get(col, float("nan"))
            frame[col] = series.clip(lower=lo, upper=hi) if np.isfinite(lo) and np.isfinite(hi) else series
        return frame


def _ensure_sorted(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    out["trade_date"] = out["trade_date"].astype(str)
    return out


def _rows_for_days(df: pd.DataFrame, days: Sequence[str]) -> pd.DataFrame:
    return df[df["trade_date"].astype(str).isin({str(day) for day in days})].copy().sort_values("timestamp").reset_index(drop=True)


def _compute_metrics(y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5) -> Dict[str, Optional[float]]:
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)
    y_pred = (y_prob >= float(threshold)).astype(int)
    has_both = len(np.unique(y_true)) >= 2
    brier = float(brier_score_loss(y_true, y_prob)) if len(y_true) else 0.0
    return {
        "rmse": float(np.sqrt(brier)),
        "brier": brier,
        "accuracy": float(accuracy_score(y_true, y_pred)) if len(y_true) else 0.0,
        "precision": float(precision_score(y_true, y_pred, zero_division=0)) if len(y_true) else 0.0,
        "recall": float(recall_score(y_true, y_pred, zero_division=0)) if len(y_true) else 0.0,
        "f1": float(f1_score(y_true, y_pred, zero_division=0)) if len(y_true) else 0.0,
        "roc_auc": float(roc_auc_score(y_true, y_prob)) if has_both else None,
        "pr_auc": float(average_precision_score(y_true, y_prob)) if has_both else None,
        "positive_rate": float(np.mean(y_true)) if len(y_true) else 0.0,
        "prediction_rate": float(np.mean(y_pred)) if len(y_true) else 0.0,
    }


def _aggregate_metric_rows(rows: Sequence[Dict[str, Optional[float]]]) -> Dict[str, Optional[float]]:
    if not rows:
        return {}
    keys = sorted(set().union(*[set(row.keys()) for row in rows]))
    out: Dict[str, Optional[float]] = {}
    for key in keys:
        numeric = [float(row[key]) for row in rows if row.get(key) is not None and np.isfinite(float(row[key]))]
        out[f"{key}_mean"] = float(np.mean(numeric)) if numeric else None
        out[f"{key}_std"] = float(np.std(numeric)) if numeric else None
    return out


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except Exception:
        return float("nan")


def _trade_side(ce_prob: float, pe_prob: float, ce_thr: float, pe_thr: float) -> Optional[str]:
    ce_ok = float(ce_prob) >= float(ce_thr)
    pe_ok = float(pe_prob) >= float(pe_thr)
    if ce_ok and pe_ok:
        return "CE" if float(ce_prob) >= float(pe_prob) else "PE"
    if ce_ok:
        return "CE"
    if pe_ok:
        return "PE"
    return None


def _max_drawdown_pct(net_returns: Sequence[float], *, risk_per_trade_pct: float, stop_loss_pct: float) -> float:
    if not net_returns:
        return 0.0
    scale = float(risk_per_trade_pct) / max(float(stop_loss_pct), 1e-12)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in net_returns:
        equity = max(equity * (1.0 + max(float(value) * scale, -0.99)), 1e-9)
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity / peak) - 1.0)
    return float(abs(max_dd))


def _path_reason_return(row: pd.Series, side: str, cfg: TradingObjectiveConfig) -> Optional[float]:
    prefix = "ce" if str(side).upper() == "CE" else "pe"
    realized = _safe_float(row.get(f"{prefix}_realized_return"))
    reason = str(row.get(f"{prefix}_path_exit_reason", "")).strip().lower()
    if np.isfinite(realized):
        return None if reason == "time_stop" and bool(cfg.discard_time_stop) else float(realized)
    if reason in {"tp", "tp_sl_same_bar"}:
        return float(cfg.take_profit_pct)
    if reason == "sl":
        return -float(cfg.stop_loss_pct)
    if reason == "time_stop" and bool(cfg.discard_time_stop):
        return None
    forward = _safe_float(row.get(f"{prefix}_forward_return"))
    return float(forward) if np.isfinite(forward) else None


def _evaluate_trade_utility(base_df: pd.DataFrame, folds: Sequence[Dict[str, Sequence[str]]], ce_scores: Dict[int, pd.DataFrame], pe_scores: Dict[int, pd.DataFrame], cfg: TradingObjectiveConfig) -> Dict[str, object]:
    all_net_returns: List[float] = []
    fold_rows: List[Dict[str, object]] = []
    for fold_idx, fold in enumerate(folds, start=1):
        actual = _rows_for_days(base_df, fold["test_days"])
        ce_df = ce_scores.get(fold_idx)
        pe_df = pe_scores.get(fold_idx)
        if len(actual) == 0 or ce_df is None or pe_df is None:
            fold_rows.append({"fold_index": int(fold_idx), "fold_ok": False, "days": fold, "error": "missing aligned fold data"})
            continue
        merged = actual.loc[:, ["timestamp", "trade_date", "ce_path_exit_reason", "pe_path_exit_reason", "ce_forward_return", "pe_forward_return"]].merge(ce_df, on=["timestamp", "trade_date"], how="inner").merge(pe_df, on=["timestamp", "trade_date"], how="inner")
        fold_nets: List[float] = []
        for row in merged.itertuples(index=False):
            payload = pd.Series(row._asdict())
            side = _trade_side(_safe_float(payload.get("ce_prob")), _safe_float(payload.get("pe_prob")), float(cfg.ce_threshold), float(cfg.pe_threshold))
            if side is None:
                continue
            gross = _path_reason_return(payload, side=side, cfg=cfg)
            if gross is None:
                continue
            fold_nets.append(float(gross - float(cfg.cost_per_trade)))
        all_net_returns.extend(fold_nets)
        fold_rows.append({"fold_index": int(fold_idx), "fold_ok": True, "days": fold, "trades": int(len(fold_nets)), "net_return_sum": float(sum(fold_nets)), "mean_net_return_per_trade": float(np.mean(fold_nets)) if fold_nets else 0.0, "profit_factor": float(profit_factor(fold_nets)), "max_drawdown_pct": float(_max_drawdown_pct(fold_nets, risk_per_trade_pct=float(cfg.risk_per_trade_pct), stop_loss_pct=float(cfg.stop_loss_pct))), "win_rate": float(np.mean(np.asarray(fold_nets) > 0.0)) if fold_nets else 0.0})
    total_pf = float(profit_factor(all_net_returns))
    total_dd_pct = float(_max_drawdown_pct(all_net_returns, risk_per_trade_pct=float(cfg.risk_per_trade_pct), stop_loss_pct=float(cfg.stop_loss_pct)))
    trades_total = int(len(all_net_returns))
    return {
        "config": cfg.to_dict(),
        "trades_total": trades_total,
        "net_return_sum": float(sum(all_net_returns)),
        "mean_net_return_per_trade": float(np.mean(all_net_returns)) if all_net_returns else 0.0,
        "profit_factor": total_pf,
        "max_drawdown_pct": total_dd_pct,
        "win_rate": float(np.mean(np.asarray(all_net_returns) > 0.0)) if all_net_returns else 0.0,
        "constraints_pass": bool(trades_total >= int(cfg.min_trades) and total_pf >= float(cfg.min_profit_factor) and total_dd_pct <= float(cfg.max_equity_drawdown_pct)),
        "folds": fold_rows,
    }


def _build_model(model_spec: ModelSpec, random_state: int, preprocess_cfg: PreprocessConfig, model_n_jobs: int = 1) -> Pipeline:
    family = str(model_spec.family).strip().lower()
    params = dict(model_spec.params or {})
    resolved_n_jobs = max(1, int(model_n_jobs))
    if family == "logreg":
        return Pipeline(steps=[("clipper", QuantileClipper(preprocess_cfg.clip_lower_q, preprocess_cfg.clip_upper_q)), ("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler(with_mean=True, with_std=True)), ("model", LogisticRegression(C=float(params.get("c", 1.0)), class_weight=params.get("class_weight"), random_state=int(random_state), max_iter=int(params.get("max_iter", 1000)), solver=str(params.get("solver", "lbfgs"))))])
    if family == "xgb":
        return Pipeline(steps=[("clipper", QuantileClipper(preprocess_cfg.clip_lower_q, preprocess_cfg.clip_upper_q)), ("imputer", SimpleImputer(strategy="median")), ("model", XGBClassifier(objective="binary:logistic", eval_metric="logloss", random_state=int(random_state), seed=int(random_state), n_jobs=resolved_n_jobs, tree_method="hist", verbosity=0, max_depth=int(params.get("max_depth", 4)), n_estimators=int(params.get("n_estimators", 300)), learning_rate=float(params.get("learning_rate", 0.03)), subsample=float(params.get("subsample", 1.0)), colsample_bytree=float(params.get("colsample_bytree", 1.0)), reg_alpha=float(params.get("reg_alpha", 0.0)), reg_lambda=float(params.get("reg_lambda", 1.0))))])
    if family == "lgbm":
        if LGBMClassifier is None:
            raise RuntimeError("LightGBM is required for lgbm models")
        return Pipeline(steps=[("clipper", QuantileClipper(preprocess_cfg.clip_lower_q, preprocess_cfg.clip_upper_q)), ("imputer", SimpleImputer(strategy="median")), ("model", LGBMClassifier(objective="binary", random_state=int(random_state), n_jobs=resolved_n_jobs, verbosity=-1, boosting_type=str(params.get("boosting_type", "gbdt")), num_leaves=int(params.get("num_leaves", 31)), max_depth=int(params.get("max_depth", -1)), n_estimators=int(params.get("n_estimators", 300)), learning_rate=float(params.get("learning_rate", 0.03)), subsample=float(params.get("subsample", 1.0)), colsample_bytree=float(params.get("colsample_bytree", 1.0)), reg_alpha=float(params.get("reg_alpha", 0.0)), reg_lambda=float(params.get("reg_lambda", 0.0)), min_child_samples=int(params.get("min_child_samples", 20)), class_weight=params.get("class_weight")))])
    raise ValueError(f"unsupported model family: {model_spec.family}")


def _apply_feature_set(base_columns: Sequence[str], feature_set_name: str) -> List[str]:
    import re

    spec = feature_set_specs_by_name()[feature_set_name]
    cols = list(base_columns)
    if spec.include_regex:
        cols = [col for col in cols if any(re.search(pattern, col) for pattern in spec.include_regex)]
    if spec.exclude_regex:
        cols = [col for col in cols if not any(re.search(pattern, col) for pattern in spec.exclude_regex)]
    return cols


def _filter_features_by_missing_rate(df: pd.DataFrame, columns: Sequence[str], max_missing_rate: float) -> Tuple[List[str], List[Dict[str, float]]]:
    kept: List[str] = []
    dropped: List[Dict[str, float]] = []
    for col in columns:
        miss_rate = 1.0 if col not in df.columns else float(df[col].isna().mean())
        if miss_rate > float(max_missing_rate):
            dropped.append({"feature": str(col), "missing_rate": miss_rate})
        else:
            kept.append(str(col))
    return kept, sorted(dropped, key=lambda row: row["missing_rate"], reverse=True)


def _prepare_side_df(df: pd.DataFrame, side: str, label_target: str) -> pd.DataFrame:
    mode = str(label_target).strip().lower()
    out = df[df[f"{side}_label_valid"] == 1.0].copy()
    if mode == LABEL_TARGET_BASE:
        out["target"] = pd.to_numeric(out[f"{side}_label"], errors="coerce")
    elif mode == LABEL_TARGET_PATH_TP_SL:
        reason = out[f"{side}_path_exit_reason"].astype(str).str.lower()
        out["target"] = np.where(reason.isin({"tp", "tp_sl_same_bar"}), 1.0, np.where(reason == "sl", 0.0, np.nan))
    elif mode == LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO:
        reason = out[f"{side}_path_exit_reason"].astype(str).str.lower()
        out["target"] = np.where(reason.isin({"tp", "tp_sl_same_bar"}), 1.0, np.where(reason.isin({"sl", "time_stop"}), 0.0, np.nan))
    elif mode == LABEL_TARGET_PATH_TP_SL_RESOLVED_ONLY:
        out = out[out[f"{side}_path_target_valid"].fillna(0.0) == 1.0].copy()
        reason = out[f"{side}_path_exit_reason"].astype(str).str.lower()
        out["target"] = np.where(reason.isin({"tp", "tp_sl_same_bar"}), 1.0, np.where(reason == "sl", 0.0, np.nan))
    else:
        raise ValueError(f"unsupported label_target: {label_target}")
    return out[out["target"].notna()].assign(target=lambda frame: frame["target"].astype(int)).sort_values("timestamp").reset_index(drop=True)


def _is_move_label_target(label_target: str) -> bool:
    return str(label_target).strip().lower() in {LABEL_TARGET_MOVE_BARRIER_HIT, LABEL_TARGET_MOVE_DIRECTION_UP}


def _single_target_meta(label_target: str) -> Dict[str, str]:
    normalized = str(label_target).strip().lower()
    if normalized == LABEL_TARGET_MOVE_BARRIER_HIT:
        return {
            "model_key": "move",
            "prob_col": "move_prob",
            "prediction_mode": "move",
            "event_end_col": "move_event_end_ts",
        }
    if normalized == LABEL_TARGET_MOVE_DIRECTION_UP:
        return {
            "model_key": "direction",
            "prob_col": "direction_up_prob",
            "prediction_mode": "direction_up",
            "event_end_col": "move_event_end_ts",
        }
    raise ValueError(f"unsupported single-target label_target: {label_target}")


def _prepare_single_target_df(df: pd.DataFrame, label_target: str) -> pd.DataFrame:
    normalized = str(label_target).strip().lower()
    out = df[pd.to_numeric(df.get("move_label_valid"), errors="coerce").fillna(0.0) == 1.0].copy()
    if normalized == LABEL_TARGET_MOVE_BARRIER_HIT:
        if "move_label" not in out.columns:
            raise ValueError("move label target requires move_label column")
        out["target"] = pd.to_numeric(out["move_label"], errors="coerce")
        return out[out["target"].notna()].assign(target=lambda frame: frame["target"].astype(int)).sort_values("timestamp").reset_index(drop=True)
    if normalized == LABEL_TARGET_MOVE_DIRECTION_UP:
        if "move_label" not in out.columns or "move_first_hit_side" not in out.columns:
            raise ValueError("move direction target requires move_label and move_first_hit_side columns")
        out = out[pd.to_numeric(out["move_label"], errors="coerce").fillna(0.0) == 1.0].copy()
        direction = out["move_first_hit_side"].astype(str).str.strip().str.lower()
        out["target"] = np.where(direction == "up", 1.0, np.where(direction == "down", 0.0, np.nan))
        return out[out["target"].notna()].assign(target=lambda frame: frame["target"].astype(int)).sort_values("timestamp").reset_index(drop=True)
    raise ValueError(f"unsupported single-target label_target: {label_target}")


def _prepare_move_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _prepare_single_target_df(df, LABEL_TARGET_MOVE_BARRIER_HIT)
    return out[out["target"].notna()].assign(target=lambda frame: frame["target"].astype(int)).sort_values("timestamp").reset_index(drop=True)


def _fit_model_for_fold(train_df: pd.DataFrame, feature_columns: Sequence[str], model_spec: ModelSpec, random_state: int, preprocess_cfg: PreprocessConfig, model_n_jobs: int) -> object:
    y_train = train_df["target"].astype(int).to_numpy()
    classes = np.unique(y_train)
    if len(classes) < 2:
        return ConstantProbModel(float(classes[0]) if len(classes) == 1 else 0.0)
    model = _build_model(model_spec, random_state=random_state, preprocess_cfg=preprocess_cfg, model_n_jobs=model_n_jobs)
    model.fit(train_df.loc[:, list(feature_columns)], y_train)
    return model


def _predict_with_fold_model(model: object, score_df: pd.DataFrame, feature_columns: Sequence[str]) -> np.ndarray:
    return model.predict_proba(score_df.loc[:, list(feature_columns)])[:, 1]


def _move_event_end_col(df: pd.DataFrame, fallback: object = None) -> str:
    meta_col = _single_target_meta(LABEL_TARGET_MOVE_BARRIER_HIT)["event_end_col"]
    for candidate in (
        str(fallback).strip() if fallback is not None else "",
        meta_col,
        "long_event_end_ts",
        "ce_event_end_ts",
    ):
        if candidate and candidate in df.columns:
            return candidate
    raise ValueError("move label target requires an event end timestamp column for event-overlap purge")


def _evaluate_move_experiment(
    df: pd.DataFrame,
    feature_columns: Sequence[str],
    model_spec: ModelSpec,
    cv_config: Dict[str, Any],
    random_state: int,
    preprocess_cfg: PreprocessConfig,
    label_target: str,
    model_n_jobs: int,
    return_utility_score_payload: bool = False,
) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
    days = sorted(df["trade_date"].astype(str).unique().tolist())
    folds = build_day_folds(
        days=days,
        train_days=int(cv_config["train_days"]),
        valid_days=int(cv_config["valid_days"]),
        test_days=int(cv_config["test_days"]),
        step_days=int(cv_config["step_days"]),
        purge_days=int(cv_config.get("purge_days", 0)),
        embargo_days=int(cv_config.get("embargo_days", 0)),
    )
    meta = _single_target_meta(label_target)
    single_df = _prepare_single_target_df(df, label_target)
    fold_details: List[Dict[str, object]] = []
    valid_metrics_rows: List[Dict[str, Optional[float]]] = []
    test_metrics_rows: List[Dict[str, Optional[float]]] = []
    combined_fold_rows: List[Dict[str, Optional[float]]] = []
    move_scores: Dict[int, pd.DataFrame] = {}
    normalized_purge_mode = normalize_purge_mode(cv_config.get("purge_mode", PURGE_MODE_DAYS))
    for fold_idx, fold in enumerate(folds, start=1):
        train_df = _rows_for_days(single_df, fold["train_days"])
        valid_df = _rows_for_days(single_df, fold["valid_days"])
        test_df = _rows_for_days(single_df, fold["test_days"])
        if normalized_purge_mode == PURGE_MODE_EVENT_OVERLAP:
            end_col = _move_event_end_col(single_df, fallback=cv_config.get("event_end_col"))
            train_df = apply_event_overlap_purge(
                train_df,
                heldout_frames=[valid_df, test_df],
                event_end_col=end_col,
                embargo_rows=int(cv_config.get("embargo_rows", 0)),
            )
        if len(train_df) == 0 or len(valid_df) == 0 or len(test_df) == 0:
            fold_details.append({"fold_ok": False, "days": fold, "error": "empty partition"})
            continue
        score_df = _rows_for_days(single_df, fold["test_days"])
        fold_model = _fit_model_for_fold(train_df, feature_columns, model_spec, random_state, preprocess_cfg, model_n_jobs)
        valid_prob = _predict_with_fold_model(fold_model, valid_df, feature_columns)
        test_prob = _predict_with_fold_model(fold_model, test_df, feature_columns)
        score_prob = _predict_with_fold_model(fold_model, score_df, feature_columns)
        move_scores[fold_idx] = score_df.loc[:, ["timestamp", "trade_date"]].assign(**{meta["prob_col"]: score_prob})
        valid_metrics = _compute_metrics(valid_df["target"].astype(int).to_numpy(), valid_prob)
        test_metrics = _compute_metrics(test_df["target"].astype(int).to_numpy(), test_prob)
        valid_metrics_rows.append(valid_metrics)
        test_metrics_rows.append(test_metrics)
        fold_details.append(
            {
                "fold_ok": True,
                "days": fold,
                "rows": {"train": int(len(train_df)), "valid": int(len(valid_df)), "test": int(len(test_df))},
                "metrics": {"valid": valid_metrics, "test": test_metrics},
            }
        )
        combined_fold_rows.append({"rmse": test_metrics.get("rmse"), "brier": test_metrics.get("brier")})
    result = {
        "prediction_mode": meta["prediction_mode"],
        "fold_count": int(len(folds)),
        meta["model_key"]: {
            "fold_count": int(len(folds)),
            "fold_ok_count": int(sum(1 for row in fold_details if row.get("fold_ok"))),
            "folds": fold_details,
            "aggregate": {
                "valid": _aggregate_metric_rows(valid_metrics_rows),
                "test": _aggregate_metric_rows(test_metrics_rows),
            },
        },
        "combined_test": _aggregate_metric_rows(combined_fold_rows),
        "trading_utility": None,
    }
    payload = {"folds": folds, f"{meta['model_key']}_scores": move_scores} if return_utility_score_payload else None
    return result, payload


def _evaluate_experiment(df: pd.DataFrame, feature_columns: Sequence[str], model_spec: ModelSpec, cv_config: Dict[str, Any], random_state: int, preprocess_cfg: PreprocessConfig, label_target: str, utility_cfg: TradingObjectiveConfig, model_n_jobs: int, return_utility_score_payload: bool = False) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
    if _is_move_label_target(label_target):
        return _evaluate_move_experiment(
            df,
            feature_columns,
            model_spec,
            cv_config,
            random_state,
            preprocess_cfg,
            label_target,
            model_n_jobs,
            return_utility_score_payload=return_utility_score_payload,
        )
    days = sorted(df["trade_date"].astype(str).unique().tolist())
    folds = build_day_folds(days=days, train_days=int(cv_config["train_days"]), valid_days=int(cv_config["valid_days"]), test_days=int(cv_config["test_days"]), step_days=int(cv_config["step_days"]), purge_days=int(cv_config.get("purge_days", 0)), embargo_days=int(cv_config.get("embargo_days", 0)))
    side_reports: Dict[str, object] = {}
    ce_scores: Dict[int, pd.DataFrame] = {}
    pe_scores: Dict[int, pd.DataFrame] = {}
    combined_fold_rows: List[Dict[str, Optional[float]]] = []
    normalized_purge_mode = normalize_purge_mode(cv_config.get("purge_mode", PURGE_MODE_DAYS))
    for side in ("ce", "pe"):
        side_df = _prepare_side_df(df, side, label_target=label_target)
        fold_details: List[Dict[str, object]] = []
        valid_metrics_rows: List[Dict[str, Optional[float]]] = []
        test_metrics_rows: List[Dict[str, Optional[float]]] = []
        for fold_idx, fold in enumerate(folds, start=1):
            train_df = _rows_for_days(side_df, fold["train_days"])
            valid_df = _rows_for_days(side_df, fold["valid_days"])
            test_df = _rows_for_days(side_df, fold["test_days"])
            if normalized_purge_mode == PURGE_MODE_EVENT_OVERLAP:
                end_col = infer_side_event_end_col(side_df, side=side, fallback=cv_config.get("event_end_col"))
                train_df = apply_event_overlap_purge(train_df, heldout_frames=[valid_df, test_df], event_end_col=end_col, embargo_rows=int(cv_config.get("embargo_rows", 0)))
            if len(train_df) == 0 or len(valid_df) == 0 or len(test_df) == 0:
                fold_details.append({"fold_ok": False, "days": fold, "error": "empty partition"})
                continue
            utility_df = _rows_for_days(df, fold["test_days"])
            fold_model = _fit_model_for_fold(train_df, feature_columns, model_spec, random_state, preprocess_cfg, model_n_jobs)
            valid_prob = _predict_with_fold_model(fold_model, valid_df, feature_columns)
            test_prob = _predict_with_fold_model(fold_model, test_df, feature_columns)
            utility_prob = _predict_with_fold_model(fold_model, utility_df, feature_columns)
            score_frame = utility_df.loc[:, ["timestamp", "trade_date"]].copy()
            score_frame[f"{side}_prob"] = utility_prob
            if side == "ce":
                ce_scores[fold_idx] = score_frame
            else:
                pe_scores[fold_idx] = score_frame
            valid_metrics = _compute_metrics(valid_df["target"].astype(int).to_numpy(), valid_prob)
            test_metrics = _compute_metrics(test_df["target"].astype(int).to_numpy(), test_prob)
            valid_metrics_rows.append(valid_metrics)
            test_metrics_rows.append(test_metrics)
            fold_details.append({"fold_ok": True, "days": fold, "rows": {"train": int(len(train_df)), "valid": int(len(valid_df)), "test": int(len(test_df))}, "metrics": {"valid": valid_metrics, "test": test_metrics}})
            combined_fold_rows.append({"rmse": test_metrics.get("rmse"), "brier": test_metrics.get("brier")})
        side_reports[side] = {"fold_count": int(len(folds)), "fold_ok_count": int(sum(1 for row in fold_details if row.get("fold_ok"))), "folds": fold_details, "aggregate": {"valid": _aggregate_metric_rows(valid_metrics_rows), "test": _aggregate_metric_rows(test_metrics_rows)}}
    utility_summary = _evaluate_trade_utility(df, folds, ce_scores, pe_scores, utility_cfg)
    result = {"fold_count": int(len(folds)), "ce": side_reports["ce"], "pe": side_reports["pe"], "combined_test": _aggregate_metric_rows(combined_fold_rows), "trading_utility": utility_summary}
    payload = {"folds": folds, "ce_scores": ce_scores, "pe_scores": pe_scores} if return_utility_score_payload else None
    return result, payload


def _objective_value(experiment_result: Dict[str, object], objective: str) -> Optional[float]:
    obj = str(objective).strip().lower()
    if obj in {"rmse", "brier"}:
        return experiment_result["combined_test"].get(f"{obj}_mean")
    if obj == "trade_utility":
        utility = experiment_result.get("trading_utility") or {}
        if not bool(utility.get("constraints_pass", False)):
            return None
        value = _safe_float(utility.get("net_return_sum"))
        return float(value) if np.isfinite(value) else None
    raise ValueError(f"unsupported objective: {objective}")


def _fallback_objective_value(experiment_result: Dict[str, object], objective: str) -> Optional[float]:
    if str(objective).strip().lower() == "trade_utility":
        value = _safe_float((experiment_result.get("trading_utility") or {}).get("net_return_sum"))
        return float(value) if np.isfinite(value) else None
    return _objective_value(experiment_result, objective)


def _is_better(candidate: Dict[str, object], incumbent: Dict[str, object], objective: str) -> bool:
    minimize = str(objective).strip().lower() in {"rmse", "brier"}
    c_val = candidate.get("objective_value")
    i_val = incumbent.get("objective_value")
    if c_val is None:
        return False
    if i_val is None:
        return True
    if minimize and c_val != i_val:
        return float(c_val) < float(i_val)
    if (not minimize) and c_val != i_val:
        return float(c_val) > float(i_val)
    if int(candidate.get("feature_count", 10**9)) != int(incumbent.get("feature_count", 10**9)):
        return int(candidate.get("feature_count", 10**9)) < int(incumbent.get("feature_count", 10**9))
    return str(candidate.get("experiment_id", "")) < str(incumbent.get("experiment_id", ""))


def _fit_final_models(labeled_df: pd.DataFrame, feature_columns: Sequence[str], model_spec: ModelSpec, random_state: int, preprocess_cfg: PreprocessConfig, label_target: str, model_n_jobs: int) -> Dict[str, object]:
    if _is_move_label_target(label_target):
        meta = _single_target_meta(label_target)
        single_df = _prepare_single_target_df(labeled_df, label_target)
        y = single_df["target"].astype(int).to_numpy()
        classes = np.unique(y)
        if len(classes) < 2:
            return {meta["model_key"]: ConstantProbModel(float(classes[0]) if len(classes) == 1 else 0.0)}
        model = _build_model(model_spec, random_state=random_state, preprocess_cfg=preprocess_cfg, model_n_jobs=model_n_jobs)
        model.fit(single_df.loc[:, list(feature_columns)], y)
        return {meta["model_key"]: model}
    models: Dict[str, object] = {}
    for side in ("ce", "pe"):
        side_df = _prepare_side_df(labeled_df, side, label_target=label_target)
        y = side_df["target"].astype(int).to_numpy()
        classes = np.unique(y)
        if len(classes) < 2:
            models[side] = ConstantProbModel(float(classes[0]) if len(classes) == 1 else 0.0)
            continue
        model = _build_model(model_spec, random_state=random_state, preprocess_cfg=preprocess_cfg, model_n_jobs=model_n_jobs)
        model.fit(side_df.loc[:, list(feature_columns)], y)
        models[side] = model
    return models


def _build_model_package(created_at_utc: str, feature_profile: str, objective: str, label_target: str, feature_columns: Sequence[str], feature_set: str, model_meta: Dict[str, object], cv_config: Dict[str, object], preprocessing: Dict[str, object], runtime_config: Dict[str, object], trading_utility_config: Dict[str, object], models: Dict[str, object]) -> Dict[str, object]:
    single_target = _single_target_meta(label_target) if _is_move_label_target(label_target) else None
    return {"kind": "ml_pipeline_2_research_model_package", "created_at_utc": created_at_utc, "feature_profile": str(feature_profile), "objective": str(objective), "label_target": str(label_target), "prediction_mode": (single_target["prediction_mode"] if single_target is not None else "directional"), "single_target": single_target, "feature_columns": list(feature_columns), "selected_feature_set": str(feature_set), "selected_model": dict(model_meta), "cv_config": dict(cv_config), "preprocessing": dict(preprocessing), "runtime": dict(runtime_config), "trading_utility_config": dict(trading_utility_config), "models": models, "_model_input_contract": {"required_features": list(feature_columns), "missing_policy": "error", "source": "feature_columns"}}


def _build_leaderboard(experiments: Sequence[Dict[str, object]], objective: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for experiment in experiments:
        result = experiment["result"]
        utility = result.get("trading_utility") or {}
        rows.append({"experiment_id": experiment["experiment_id"], "feature_set": experiment["feature_set"], "model_name": experiment["model"]["name"], "model_family": experiment["model"]["family"], "feature_count": int(experiment["feature_count"]), "objective": str(objective), "objective_value": experiment.get("objective_value"), "utility_net_return_sum": utility.get("net_return_sum"), "utility_profit_factor": utility.get("profit_factor"), "utility_trades_total": utility.get("trades_total"), "utility_constraints_pass": utility.get("constraints_pass")})
    minimize = str(objective).strip().lower() in {"rmse", "brier"}
    return sorted(rows, key=lambda row: ((float("inf") if row["objective_value"] is None else float(row["objective_value"])) if minimize else -(float("-inf") if row["objective_value"] is None else float(row["objective_value"])), int(row["feature_count"]), str(row["experiment_id"])))


def run_training_cycle_catalog(labeled_df: pd.DataFrame, *, feature_profile: str = "all", objective: str = "trade_utility", random_state: int = 42, max_experiments: Optional[int] = None, preprocess_cfg: Optional[PreprocessConfig] = None, label_target: str = LABEL_TARGET_BASE, utility_cfg: Optional[TradingObjectiveConfig] = None, model_whitelist: Optional[Sequence[str]] = None, feature_set_whitelist: Optional[Sequence[str]] = None, progress_callback: Optional[Callable[[Dict[str, object]], None]] = None, retain_utility_score_payload: bool = False, fit_all_final_models: bool = False, model_n_jobs: int = 1, **cv_kwargs: Any) -> Dict[str, object]:
    frame = _ensure_sorted(labeled_df)
    if str(label_target).strip().lower() not in LABEL_TARGET_CHOICES:
        raise ValueError(f"unsupported label_target: {label_target}")
    if _is_move_label_target(label_target) and str(objective).strip().lower() == "trade_utility":
        raise ValueError("move_barrier_hit does not support trade_utility objective; use brier or rmse")
    effective_preprocess = preprocess_cfg or PreprocessConfig()
    effective_utility = utility_cfg or TradingObjectiveConfig()
    effective_model_n_jobs = max(1, int(model_n_jobs))
    base_features = select_feature_columns(frame, feature_profile=feature_profile)
    if not base_features:
        raise ValueError("no base features for training cycle")
    base_features, dropped_by_missing = _filter_features_by_missing_rate(frame, base_features, float(effective_preprocess.max_missing_rate))
    if not base_features:
        raise ValueError("all features dropped by preprocessing missing-rate gate")
    feature_names = [spec.name for spec in DEFAULT_FEATURE_SET_SPECS]
    model_names = [spec.name for spec in DEFAULT_MODEL_SPECS]
    if feature_set_whitelist:
        unknown = sorted(set(feature_set_whitelist) - set(feature_names))
        if unknown:
            raise ValueError(f"unknown feature_set: {unknown}; valid options: {sorted(feature_names)}")
        feature_names = [name for name in feature_names if name in set(feature_set_whitelist)]
    if model_whitelist:
        unknown = sorted(set(model_whitelist) - set(model_names))
        if unknown:
            raise ValueError(f"unknown model: {unknown}; valid options: {sorted(model_names)}")
        model_names = [name for name in model_names if name in set(model_whitelist)]
    cv_config = {"train_days": int(cv_kwargs.get("train_days")), "valid_days": int(cv_kwargs.get("valid_days")), "test_days": int(cv_kwargs.get("test_days")), "step_days": int(cv_kwargs.get("step_days")), "purge_days": int(cv_kwargs.get("purge_days", 0)), "embargo_days": int(cv_kwargs.get("embargo_days", 0)), "purge_mode": normalize_purge_mode(cv_kwargs.get("purge_mode", PURGE_MODE_DAYS)), "embargo_rows": int(cv_kwargs.get("embargo_rows", 0)), "event_end_col": cv_kwargs.get("event_end_col")}
    preprocessing = {"max_missing_rate": float(effective_preprocess.max_missing_rate), "clip_lower_q": float(effective_preprocess.clip_lower_q), "clip_upper_q": float(effective_preprocess.clip_upper_q), "dropped_features_by_missing_rate": dropped_by_missing, "features_after_preprocess_gate": int(len(base_features))}
    runtime_config = {"model_n_jobs": int(effective_model_n_jobs)}
    if callable(progress_callback):
        progress_callback({"phase": "training_cycle", "event": "search_space", "feature_sets": feature_names, "models": model_names, "experiments_total": int(min(len(feature_names) * len(model_names), max_experiments) if max_experiments is not None else len(feature_names) * len(model_names))})
    experiments: List[Dict[str, object]] = []
    experiment_counter = 0
    max_exp = int(max_experiments) if max_experiments is not None else None
    for feature_set_name in feature_names:
        selected_features = _apply_feature_set(base_features, feature_set_name)
        if not selected_features:
            continue
        for model_name in model_names:
            experiment_counter += 1
            if max_exp is not None and experiment_counter > max_exp:
                break
            model_spec = model_specs_by_name()[model_name]
            experiment_id = f"{feature_set_name}__{model_name}"
            if callable(progress_callback):
                progress_callback({"phase": "training_cycle", "event": "experiment_start", "experiment_index": int(experiment_counter), "experiment_id": experiment_id, "feature_set": feature_set_name, "model": model_name})
            result, utility_score_payload = _evaluate_experiment(frame, selected_features, model_spec, cv_config, random_state, effective_preprocess, label_target, effective_utility, effective_model_n_jobs, return_utility_score_payload=retain_utility_score_payload)
            experiments.append({"experiment_id": experiment_id, "feature_set": feature_set_name, "model": model_spec.to_dict(), "feature_count": int(len(selected_features)), "selected_features": list(selected_features), "result": result, "objective_value": _objective_value(result, objective), "fallback_objective_value": _fallback_objective_value(result, objective), "utility_score_payload": utility_score_payload})
        if max_exp is not None and experiment_counter >= max_exp:
            break
    if not experiments:
        raise ValueError("no experiments evaluated")
    promotable = [experiment for experiment in experiments if experiment.get("objective_value") is not None]
    best = promotable[0] if promotable else experiments[0]
    for experiment in (promotable[1:] if promotable else experiments[1:]):
        if _is_better(experiment, best, objective):
            best = experiment
    if best.get("objective_value") is None:
        best = {**best, "selected_by_fallback": True}
    selected_model_spec = model_specs_by_name()[best["model"]["name"]]
    created_at_utc = datetime.now(timezone.utc).isoformat()
    best_package = _build_model_package(created_at_utc, feature_profile, objective, label_target, best["selected_features"], best["feature_set"], best["model"], cv_config, preprocessing, runtime_config, effective_utility.to_dict(), _fit_final_models(frame, best["selected_features"], selected_model_spec, random_state, effective_preprocess, label_target, effective_model_n_jobs))
    bundles = [{"experiment_id": best["experiment_id"], "model_package": best_package, "training_result": best["result"], "utility_score_payload": best.get("utility_score_payload")}]
    if fit_all_final_models:
        for experiment in experiments:
            if experiment["experiment_id"] == best["experiment_id"]:
                continue
            model_spec = model_specs_by_name()[experiment["model"]["name"]]
            package = _build_model_package(created_at_utc, feature_profile, objective, label_target, experiment["selected_features"], experiment["feature_set"], experiment["model"], cv_config, preprocessing, runtime_config, effective_utility.to_dict(), _fit_final_models(frame, experiment["selected_features"], model_spec, random_state, effective_preprocess, label_target, effective_model_n_jobs))
            bundles.append({"experiment_id": experiment["experiment_id"], "model_package": package, "training_result": experiment["result"], "utility_score_payload": experiment.get("utility_score_payload")})
    report = {"created_at_utc": created_at_utc, "feature_profile": str(feature_profile), "objective": str(objective), "label_target": str(label_target), "rows_total": int(len(frame)), "days_total": int(frame["trade_date"].nunique()), "experiments_total": int(len(experiments)), "best_experiment": {"experiment_id": best["experiment_id"], "feature_set": best["feature_set"], "feature_count": int(best["feature_count"]), "model": best["model"], "objective_value": best.get("objective_value"), "fallback_objective_value": best.get("fallback_objective_value"), "selected_by_fallback": bool(best.get("selected_by_fallback", False))}, "leaderboard": _build_leaderboard(experiments, objective), "preprocessing": preprocessing, "runtime": runtime_config, "cv_config": cv_config, "trading_utility_config": effective_utility.to_dict()}
    return {"report": report, "model_package": best_package, "experiment_bundles": bundles}
