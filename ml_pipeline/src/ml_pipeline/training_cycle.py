import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, average_precision_score, f1_score, precision_score, recall_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from .train_baseline import (
    FEATURE_PROFILE_CORE_V1,
    FEATURE_PROFILE_CORE_V2,
    FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    FEATURE_PROFILES,
    select_feature_columns,
)
from .walk_forward import build_day_folds

if __name__ == "__main__":
    # When executed as `python -m ml_pipeline.training_cycle`, ensure custom
    # classes are reachable under the importable module path for pickle/joblib.
    sys.modules["ml_pipeline.training_cycle"] = sys.modules[__name__]

LABEL_TARGET_BASE = "base_label"
LABEL_TARGET_PATH_TP_SL = "path_tp_sl"
LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO = "path_tp_sl_time_stop_zero"
LABEL_TARGET_CHOICES: Tuple[str, ...] = (
    LABEL_TARGET_BASE,
    LABEL_TARGET_PATH_TP_SL,
    LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO,
)


@dataclass(frozen=True)
class FeatureSetSpec:
    name: str
    exclude_regex: Tuple[str, ...] = ()
    include_regex: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelSpec:
    name: str
    family: str
    params: Dict[str, Any]


@dataclass(frozen=True)
class PreprocessConfig:
    max_missing_rate: float = 0.35
    clip_lower_q: float = 0.01
    clip_upper_q: float = 0.99


@dataclass(frozen=True)
class TradingObjectiveConfig:
    ce_threshold: float = 0.60
    pe_threshold: float = 0.60
    cost_per_trade: float = 0.0006
    min_profit_factor: float = 1.30
    max_equity_drawdown_pct: float = 0.15
    min_trades: int = 50
    take_profit_pct: float = 0.30
    stop_loss_pct: float = 0.20
    discard_time_stop: bool = False
    risk_per_trade_pct: float = 0.01


class ConstantProbModel:
    def __init__(self, p1: float):
        self.p1 = float(p1)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = int(len(x))
        p1 = np.full(n, self.p1, dtype=float)
        p0 = 1.0 - p1
        return np.column_stack([p0, p1])


class QuantileClipper(BaseEstimator, TransformerMixin):
    def __init__(self, lower_q: float = 0.01, upper_q: float = 0.99):
        self.lower_q = float(lower_q)
        self.upper_q = float(upper_q)
        self.columns_: List[str] = []
        self.lower_bounds_: Dict[str, float] = {}
        self.upper_bounds_: Dict[str, float] = {}

    def fit(self, x: pd.DataFrame, y: Optional[np.ndarray] = None) -> "QuantileClipper":
        frame = pd.DataFrame(x).copy()
        self.columns_ = [str(c) for c in frame.columns]
        self.lower_bounds_ = {}
        self.upper_bounds_ = {}
        for col in self.columns_:
            series = pd.to_numeric(frame[col], errors="coerce")
            series = series.replace([np.inf, -np.inf], np.nan).dropna()
            if len(series) == 0:
                self.lower_bounds_[col] = float("nan")
                self.upper_bounds_[col] = float("nan")
                continue
            self.lower_bounds_[col] = float(series.quantile(self.lower_q))
            self.upper_bounds_[col] = float(series.quantile(self.upper_q))
        return self

    def transform(self, x: pd.DataFrame) -> pd.DataFrame:
        frame = pd.DataFrame(x).copy()
        for col in self.columns_:
            if col not in frame.columns:
                continue
            series = pd.to_numeric(frame[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
            lo = self.lower_bounds_.get(col, float("nan"))
            hi = self.upper_bounds_.get(col, float("nan"))
            if np.isfinite(lo) and np.isfinite(hi):
                series = series.clip(lower=lo, upper=hi)
            frame[col] = series
        return frame


ConstantProbModel.__module__ = "ml_pipeline.training_cycle"
QuantileClipper.__module__ = "ml_pipeline.training_cycle"


def _ensure_sorted(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    out["trade_date"] = out["trade_date"].astype(str)
    return out


def _rows_for_days(df: pd.DataFrame, days: Sequence[str]) -> pd.DataFrame:
    allowed = {str(x) for x in days}
    out = df[df["trade_date"].astype(str).isin(allowed)].copy()
    return out.sort_values("timestamp").reset_index(drop=True)


def _compute_metrics(y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5) -> Dict[str, Optional[float]]:
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob).astype(float)
    y_pred = (y_prob >= float(threshold)).astype(int)
    brier = float(np.mean((y_prob - y_true) ** 2)) if len(y_true) else 0.0
    rmse = float(np.sqrt(brier))
    classes = np.unique(y_true)
    has_both = len(classes) >= 2
    return {
        "rmse": rmse,
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
    keys = sorted(set().union(*[set(r.keys()) for r in rows]))
    out: Dict[str, Optional[float]] = {}
    for key in keys:
        vals = [r.get(key) for r in rows]
        numeric = [float(v) for v in vals if v is not None and np.isfinite(v)]
        if not numeric:
            out[f"{key}_mean"] = None
            out[f"{key}_std"] = None
            continue
        out[f"{key}_mean"] = float(np.mean(numeric))
        out[f"{key}_std"] = float(np.std(numeric))
    return out


def _safe_float(value: object) -> float:
    try:
        if value is None:
            return float("nan")
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


def _max_drawdown(net_returns: Sequence[float]) -> float:
    if not net_returns:
        return 0.0
    s = pd.Series([float(x) for x in net_returns], dtype=float)
    cum = s.cumsum()
    dd = cum - cum.cummax()
    return float(dd.min()) if len(dd) else 0.0


def _max_drawdown_pct(
    net_returns: Sequence[float],
    *,
    risk_per_trade_pct: float,
    stop_loss_pct: float,
) -> float:
    if not net_returns:
        return 0.0
    scale = 1.0
    sl = float(stop_loss_pct)
    if np.isfinite(sl) and sl > 0.0:
        scale = float(risk_per_trade_pct) / float(sl)
    elif np.isfinite(risk_per_trade_pct):
        scale = float(risk_per_trade_pct)
    scale = max(0.0, float(scale))
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in net_returns:
        r = _safe_float(value)
        if not np.isfinite(r):
            continue
        # Convert premium return into capital return using fixed risk sizing.
        capital_r = float(r) * scale
        capital_r = max(capital_r, -0.99)
        next_equity = equity * (1.0 + capital_r)
        equity = max(next_equity, 1e-9)
        if equity > peak:
            peak = equity
        if peak > 0.0:
            dd = (equity / peak) - 1.0
            if dd < max_dd:
                max_dd = float(dd)
    return float(max_dd)


def _profit_factor(net_returns: Sequence[float]) -> float:
    gains = float(sum(x for x in net_returns if x > 0.0))
    losses = float(-sum(x for x in net_returns if x < 0.0))
    if losses <= 0.0:
        return float("inf") if gains > 0.0 else 0.0
    return float(gains / losses)


def _path_reason_return(
    row: pd.Series,
    side: str,
    cfg: TradingObjectiveConfig,
) -> Optional[float]:
    prefix = "ce" if str(side).upper() == "CE" else "pe"
    reason = str(row.get(f"{prefix}_path_exit_reason", "")).strip().lower()
    if reason in {"tp", "tp_sl_same_bar"}:
        return float(cfg.take_profit_pct)
    if reason == "sl":
        return -float(cfg.stop_loss_pct)
    if reason == "time_stop" and bool(cfg.discard_time_stop):
        return None
    fr = _safe_float(row.get(f"{prefix}_forward_return"))
    if not np.isfinite(fr):
        return None
    return float(fr)


def _evaluate_trade_utility(
    base_df: pd.DataFrame,
    folds: Sequence[Dict[str, Sequence[str]]],
    ce_scores: Dict[int, pd.DataFrame],
    pe_scores: Dict[int, pd.DataFrame],
    cfg: TradingObjectiveConfig,
) -> Dict[str, object]:
    fold_rows: List[Dict[str, object]] = []
    all_net_returns: List[float] = []
    required_cols = {
        "timestamp",
        "trade_date",
        "ce_path_exit_reason",
        "pe_path_exit_reason",
        "ce_forward_return",
        "pe_forward_return",
    }
    missing_cols = [c for c in required_cols if c not in base_df.columns]
    if missing_cols:
        return {
            "config": {
                "ce_threshold": float(cfg.ce_threshold),
                "pe_threshold": float(cfg.pe_threshold),
                "cost_per_trade": float(cfg.cost_per_trade),
                "min_profit_factor": float(cfg.min_profit_factor),
                "max_equity_drawdown_pct": float(cfg.max_equity_drawdown_pct),
                "min_trades": int(cfg.min_trades),
                "take_profit_pct": float(cfg.take_profit_pct),
                "stop_loss_pct": float(cfg.stop_loss_pct),
                "discard_time_stop": bool(cfg.discard_time_stop),
                "risk_per_trade_pct": float(cfg.risk_per_trade_pct),
            },
            "trades_total": 0,
            "net_return_sum": 0.0,
            "mean_net_return_per_trade": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "constraints_pass": False,
            "error": f"missing required columns: {','.join(sorted(missing_cols))}",
            "folds": [],
        }

    for fold_idx, fold in enumerate(folds, start=1):
        ce_df = ce_scores.get(fold_idx)
        pe_df = pe_scores.get(fold_idx)
        if ce_df is None or pe_df is None:
            fold_rows.append(
                {
                    "fold_index": int(fold_idx),
                    "fold_ok": False,
                    "days": fold,
                    "error": "missing side scores",
                }
            )
            continue
        actual = _rows_for_days(base_df, fold["test_days"]).copy()
        if len(actual) == 0:
            fold_rows.append(
                {
                    "fold_index": int(fold_idx),
                    "fold_ok": False,
                    "days": fold,
                    "error": "empty test days",
                }
            )
            continue
        actual = actual.loc[
            :,
            [
                "timestamp",
                "trade_date",
                "ce_path_exit_reason",
                "pe_path_exit_reason",
                "ce_forward_return",
                "pe_forward_return",
            ],
        ].copy()

        merged = actual.merge(ce_df, on=["timestamp", "trade_date"], how="inner")
        merged = merged.merge(pe_df, on=["timestamp", "trade_date"], how="inner")
        if len(merged) == 0:
            fold_rows.append(
                {
                    "fold_index": int(fold_idx),
                    "fold_ok": False,
                    "days": fold,
                    "error": "no aligned score rows",
                }
            )
            continue

        fold_nets: List[float] = []
        skipped_time_stop = 0
        for row in merged.itertuples(index=False):
            payload = pd.Series(row._asdict())
            side = _trade_side(
                ce_prob=_safe_float(payload.get("ce_prob")),
                pe_prob=_safe_float(payload.get("pe_prob")),
                ce_thr=float(cfg.ce_threshold),
                pe_thr=float(cfg.pe_threshold),
            )
            if side is None:
                continue
            gross = _path_reason_return(payload, side=side, cfg=cfg)
            if gross is None:
                skipped_time_stop += 1
                continue
            net = float(gross - float(cfg.cost_per_trade))
            fold_nets.append(net)

        pf = _profit_factor(fold_nets)
        max_dd_r = _max_drawdown(fold_nets)
        max_dd_pct = _max_drawdown_pct(
            fold_nets,
            risk_per_trade_pct=float(cfg.risk_per_trade_pct),
            stop_loss_pct=float(cfg.stop_loss_pct),
        )
        fold_rows.append(
            {
                "fold_index": int(fold_idx),
                "fold_ok": True,
                "days": fold,
                "trades": int(len(fold_nets)),
                "net_return_sum": float(sum(fold_nets)),
                "mean_net_return_per_trade": (float(np.mean(fold_nets)) if fold_nets else 0.0),
                "profit_factor": float(pf),
                "max_drawdown_r": float(max_dd_r),
                "max_drawdown_pct": float(max_dd_pct),
                "win_rate": (float(np.mean(np.asarray(fold_nets) > 0.0)) if fold_nets else 0.0),
                "skipped_time_stop_trades": int(skipped_time_stop),
            }
        )
        all_net_returns.extend(fold_nets)

    total_trades = int(len(all_net_returns))
    total_pf = _profit_factor(all_net_returns)
    total_dd_r = _max_drawdown(all_net_returns)
    total_dd_pct = _max_drawdown_pct(
        all_net_returns,
        risk_per_trade_pct=float(cfg.risk_per_trade_pct),
        stop_loss_pct=float(cfg.stop_loss_pct),
    )
    constraints_pass = (
        total_trades >= int(cfg.min_trades)
        and float(total_pf) >= float(cfg.min_profit_factor)
        and abs(float(total_dd_pct)) <= float(cfg.max_equity_drawdown_pct)
    )
    return {
        "config": {
            "ce_threshold": float(cfg.ce_threshold),
            "pe_threshold": float(cfg.pe_threshold),
            "cost_per_trade": float(cfg.cost_per_trade),
            "min_profit_factor": float(cfg.min_profit_factor),
            "max_equity_drawdown_pct": float(cfg.max_equity_drawdown_pct),
            "min_trades": int(cfg.min_trades),
            "take_profit_pct": float(cfg.take_profit_pct),
            "stop_loss_pct": float(cfg.stop_loss_pct),
            "discard_time_stop": bool(cfg.discard_time_stop),
            "risk_per_trade_pct": float(cfg.risk_per_trade_pct),
        },
        "trades_total": total_trades,
        "net_return_sum": float(sum(all_net_returns)),
        "mean_net_return_per_trade": (float(np.mean(all_net_returns)) if all_net_returns else 0.0),
        "profit_factor": float(total_pf),
        "max_drawdown_r": float(total_dd_r),
        "max_drawdown_pct": float(total_dd_pct),
        "win_rate": (float(np.mean(np.asarray(all_net_returns) > 0.0)) if all_net_returns else 0.0),
        "constraints_pass": bool(constraints_pass),
        "folds": fold_rows,
    }


def _build_model(model_spec: ModelSpec, random_state: int, preprocess_cfg: PreprocessConfig) -> Pipeline:
    family = str(model_spec.family).strip().lower()
    params = dict(model_spec.params or {})
    if family == "logreg":
        c = float(params.get("c", 1.0))
        class_weight = params.get("class_weight")
        model = LogisticRegression(
            C=c,
            class_weight=class_weight,
            random_state=int(random_state),
            max_iter=int(params.get("max_iter", 1000)),
            solver=str(params.get("solver", "lbfgs")),
        )
        return Pipeline(
            steps=[
                ("clipper", QuantileClipper(lower_q=preprocess_cfg.clip_lower_q, upper_q=preprocess_cfg.clip_upper_q)),
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler(with_mean=True, with_std=True)),
                ("model", model),
            ]
        )
    if family == "xgb":
        model = XGBClassifier(
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=int(random_state),
            seed=int(random_state),
            n_jobs=1,
            tree_method="hist",
            verbosity=0,
            max_depth=int(params.get("max_depth", 4)),
            n_estimators=int(params.get("n_estimators", 300)),
            learning_rate=float(params.get("learning_rate", 0.03)),
            subsample=float(params.get("subsample", 1.0)),
            colsample_bytree=float(params.get("colsample_bytree", 1.0)),
            reg_alpha=float(params.get("reg_alpha", 0.0)),
            reg_lambda=float(params.get("reg_lambda", 1.0)),
        )
        return Pipeline(
            steps=[
                ("clipper", QuantileClipper(lower_q=preprocess_cfg.clip_lower_q, upper_q=preprocess_cfg.clip_upper_q)),
                ("imputer", SimpleImputer(strategy="median")),
                ("model", model),
            ]
        )
    raise ValueError(f"unsupported model family: {model_spec.family}")


def _default_feature_specs() -> List[FeatureSetSpec]:
    return [
        FeatureSetSpec(name="fo_full"),
        FeatureSetSpec(name="fo_no_opening_range", exclude_regex=(r"^opening_range_",)),
        FeatureSetSpec(name="fo_no_time_context", exclude_regex=(r"^minute_of_day$", r"^day_of_week$", r"^minute_index$")),
        FeatureSetSpec(
            name="fo_no_oi_volume",
            exclude_regex=(
                r"^ce_oi_total$",
                r"^pe_oi_total$",
                r"^ce_volume_total$",
                r"^pe_volume_total$",
                r"^pcr_oi$",
                r"^ce_pe_oi_diff$",
                r"^ce_pe_volume_diff$",
                r"^atm_oi_change_1m$",
                r"^opt_.*_oi$",
                r"^opt_.*_volume$",
            ),
        ),
        FeatureSetSpec(
            name="fo_no_otm_levels",
            exclude_regex=(
                r"^strike_m1$",
                r"^strike_p1$",
                r"^opt_m1_",
                r"^opt_p1_",
            ),
        ),
        FeatureSetSpec(
            name="fo_atm_plus_aggregates",
            include_regex=(
                r"^fut_",
                r"^ret_",
                r"^ema_",
                r"^rsi_",
                r"^atr_",
                r"^vwap_distance$",
                r"^distance_from_day_",
                r"^minute_of_day$",
                r"^day_of_week$",
                r"^opening_range_",
                r"^opt_0_",
                r"^atm_",
                r"^ce_oi_total$",
                r"^pe_oi_total$",
                r"^ce_volume_total$",
                r"^pe_volume_total$",
                r"^pcr_oi$",
                r"^ce_pe_oi_diff$",
                r"^ce_pe_volume_diff$",
            ),
        ),
        FeatureSetSpec(
            name="fo_trend_vol_only",
            include_regex=(
                r"^fut_",
                r"^ret_",
                r"^ema_",
                r"^rsi_",
                r"^atr_",
                r"^vwap_distance$",
                r"^distance_from_day_",
                r"^minute_of_day$",
                r"^day_of_week$",
                r"^opening_range_",
            ),
        ),
        FeatureSetSpec(
            name="fo_options_structure_only",
            include_regex=(
                r"^strike_",
                r"^opt_",
                r"^atm_",
                r"^ce_oi_total$",
                r"^pe_oi_total$",
                r"^ce_volume_total$",
                r"^pe_volume_total$",
                r"^pcr_oi$",
                r"^ce_pe_oi_diff$",
                r"^ce_pe_volume_diff$",
                r"^minute_of_day$",
                r"^day_of_week$",
            ),
        ),
        FeatureSetSpec(
            name="fo_core_momentum",
            include_regex=(
                r"^fut_",
                r"^ret_",
                r"^ema_",
                r"^rsi_",
                r"^atr_",
                r"^vwap_distance$",
                r"^distance_from_day_",
                r"^minute_of_day$",
                r"^day_of_week$",
                r"^opening_range_",
                r"^atm_call_return_1m$",
                r"^atm_put_return_1m$",
                r"^ce_pe_oi_diff$",
                r"^ce_pe_volume_diff$",
            ),
        ),
    ]


def _feature_specs_for_profile(feature_profile: str) -> List[FeatureSetSpec]:
    profile = str(feature_profile or "").strip().lower()
    if profile in {FEATURE_PROFILE_CORE_V1, FEATURE_PROFILE_CORE_V2}:
        # Core profile is already intentionally minimal; avoid extra ablation variants.
        return [FeatureSetSpec(name=f"{profile}_full")]
    return _default_feature_specs()


def _default_model_specs() -> List[ModelSpec]:
    return [
        ModelSpec(name="logreg_c1", family="logreg", params={"c": 1.0, "max_iter": 1000}),
        ModelSpec(name="logreg_balanced", family="logreg", params={"c": 0.5, "class_weight": "balanced", "max_iter": 1000}),
        ModelSpec(name="xgb_fast", family="xgb", params={"max_depth": 3, "n_estimators": 220, "learning_rate": 0.05, "subsample": 0.9, "colsample_bytree": 0.9}),
        ModelSpec(name="xgb_balanced", family="xgb", params={"max_depth": 4, "n_estimators": 350, "learning_rate": 0.03, "subsample": 0.85, "colsample_bytree": 0.85, "reg_lambda": 2.0}),
    ]


def _apply_feature_set(base_columns: Sequence[str], spec: FeatureSetSpec) -> List[str]:
    import re

    cols = list(base_columns)
    if spec.include_regex:
        keep: List[str] = []
        for col in cols:
            if any(re.search(pat, col) for pat in spec.include_regex):
                keep.append(col)
        cols = keep
    if spec.exclude_regex:
        cols = [col for col in cols if not any(re.search(pat, col) for pat in spec.exclude_regex)]
    return cols


def _filter_features_by_missing_rate(
    df: pd.DataFrame,
    columns: Sequence[str],
    max_missing_rate: float,
) -> Tuple[List[str], List[Dict[str, float]]]:
    kept: List[str] = []
    dropped: List[Dict[str, float]] = []
    for col in columns:
        if col not in df.columns:
            dropped.append({"feature": str(col), "missing_rate": 1.0})
            continue
        miss_rate = float(df[col].isna().mean())
        if miss_rate > float(max_missing_rate):
            dropped.append({"feature": str(col), "missing_rate": miss_rate})
        else:
            kept.append(str(col))
    dropped = sorted(dropped, key=lambda x: x["missing_rate"], reverse=True)
    return kept, dropped


def _prepare_side_df(df: pd.DataFrame, side: str, label_target: str) -> pd.DataFrame:
    mode = str(label_target).strip().lower()
    valid_col = f"{side}_label_valid"
    if valid_col not in df.columns:
        raise ValueError(f"missing required column: {valid_col}")
    out = df[df[valid_col] == 1.0].copy()
    if mode == LABEL_TARGET_BASE:
        target_col = f"{side}_label"
        if target_col not in out.columns:
            raise ValueError(f"missing required column: {target_col}")
        out = out[out[target_col].notna()].copy()
        out["target"] = out[target_col].astype(int)
    elif mode == LABEL_TARGET_PATH_TP_SL:
        reason_col = f"{side}_path_exit_reason"
        if reason_col not in out.columns:
            raise ValueError(f"missing required column: {reason_col}")
        path_valid_col = f"{side}_path_target_valid"
        if path_valid_col in out.columns:
            out = out[out[path_valid_col].fillna(0.0) == 1.0].copy()
        reason = out[reason_col].astype(str).str.lower()
        out["target"] = np.where(
            reason.isin({"tp", "tp_sl_same_bar"}),
            1.0,
            np.where(reason == "sl", 0.0, np.nan),
        )
        out = out[out["target"].notna()].copy()
        out["target"] = out["target"].astype(int)
    elif mode == LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO:
        reason_col = f"{side}_path_exit_reason"
        if reason_col not in out.columns:
            raise ValueError(f"missing required column: {reason_col}")
        reason = out[reason_col].astype(str).str.lower()
        out["target"] = np.where(
            reason.isin({"tp", "tp_sl_same_bar"}),
            1.0,
            np.where(reason.isin({"sl", "time_stop"}), 0.0, np.nan),
        )
        out = out[out["target"].notna()].copy()
        out["target"] = out["target"].astype(int)
    else:
        raise ValueError(f"unsupported label_target: {label_target}")
    return out.sort_values("timestamp").reset_index(drop=True)


def _fit_predict_fold(
    train_df: pd.DataFrame,
    score_df: pd.DataFrame,
    side: str,
    feature_columns: Sequence[str],
    model_spec: ModelSpec,
    random_state: int,
    preprocess_cfg: PreprocessConfig,
    target_col: str = "target",
) -> np.ndarray:
    x_train = train_df.loc[:, list(feature_columns)]
    y_train = train_df[target_col].astype(int).to_numpy()
    x_score = score_df.loc[:, list(feature_columns)]

    classes = np.unique(y_train)
    if len(classes) < 2:
        constant = float(classes[0]) if len(classes) == 1 else 0.0
        return np.full(len(x_score), constant, dtype=float)

    pipe = _build_model(model_spec, random_state=random_state, preprocess_cfg=preprocess_cfg)
    pipe.fit(x_train, y_train)
    return pipe.predict_proba(x_score)[:, 1]


def _evaluate_experiment(
    df: pd.DataFrame,
    feature_columns: Sequence[str],
    model_spec: ModelSpec,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    purge_days: int,
    embargo_days: int,
    random_state: int,
    preprocess_cfg: PreprocessConfig,
    label_target: str,
    utility_cfg: TradingObjectiveConfig,
) -> Dict[str, object]:
    days = sorted(df["trade_date"].astype(str).unique().tolist())
    folds = build_day_folds(
        days=days,
        train_days=int(train_days),
        valid_days=int(valid_days),
        test_days=int(test_days),
        step_days=int(step_days),
        purge_days=int(purge_days),
        embargo_days=int(embargo_days),
    )
    side_reports: Dict[str, object] = {}
    ce_utility_scores: Dict[int, pd.DataFrame] = {}
    pe_utility_scores: Dict[int, pd.DataFrame] = {}
    combined_fold_rows: List[Dict[str, Optional[float]]] = []
    for side in ("ce", "pe"):
        side_df = _prepare_side_df(df, side, label_target=label_target)
        fold_details: List[Dict[str, object]] = []
        valid_metrics_rows: List[Dict[str, Optional[float]]] = []
        test_metrics_rows: List[Dict[str, Optional[float]]] = []
        for fold_idx, fold in enumerate(folds, start=1):
            train_df = _rows_for_days(side_df, fold["train_days"])
            valid_df = _rows_for_days(side_df, fold["valid_days"])
            test_df = _rows_for_days(side_df, fold["test_days"])
            if len(train_df) == 0 or len(valid_df) == 0 or len(test_df) == 0:
                fold_details.append({"fold_ok": False, "days": fold, "error": "empty partition"})
                continue
            valid_prob = _fit_predict_fold(
                train_df=train_df,
                score_df=valid_df,
                side=side,
                feature_columns=feature_columns,
                model_spec=model_spec,
                random_state=random_state,
                preprocess_cfg=preprocess_cfg,
                target_col="target",
            )
            test_prob = _fit_predict_fold(
                train_df=train_df,
                score_df=test_df,
                side=side,
                feature_columns=feature_columns,
                model_spec=model_spec,
                random_state=random_state,
                preprocess_cfg=preprocess_cfg,
                target_col="target",
            )
            utility_df = _rows_for_days(df, fold["test_days"])
            utility_prob = _fit_predict_fold(
                train_df=train_df,
                score_df=utility_df,
                side=side,
                feature_columns=feature_columns,
                model_spec=model_spec,
                random_state=random_state,
                preprocess_cfg=preprocess_cfg,
                target_col="target",
            )
            utility_payload = utility_df.loc[:, ["timestamp", "trade_date"]].copy()
            utility_payload[f"{side}_prob"] = utility_prob
            utility_payload["timestamp"] = pd.to_datetime(utility_payload["timestamp"], errors="coerce")
            utility_payload = utility_payload.dropna(subset=["timestamp"]).drop_duplicates(
                subset=["timestamp", "trade_date"], keep="last"
            )
            if side == "ce":
                ce_utility_scores[fold_idx] = utility_payload
            else:
                pe_utility_scores[fold_idx] = utility_payload

            y_valid = valid_df["target"].astype(int).to_numpy()
            y_test = test_df["target"].astype(int).to_numpy()
            valid_metrics = _compute_metrics(y_valid, valid_prob)
            test_metrics = _compute_metrics(y_test, test_prob)
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
        side_reports[side] = {
            "fold_count": int(len(folds)),
            "fold_ok_count": int(sum(1 for x in fold_details if x.get("fold_ok"))),
            "folds": fold_details,
            "aggregate": {
                "valid": _aggregate_metric_rows(valid_metrics_rows),
                "test": _aggregate_metric_rows(test_metrics_rows),
            },
        }
    utility_summary = _evaluate_trade_utility(
        base_df=df,
        folds=folds,
        ce_scores=ce_utility_scores,
        pe_scores=pe_utility_scores,
        cfg=utility_cfg,
    )
    return {
        "fold_count": int(len(folds)),
        "ce": side_reports["ce"],
        "pe": side_reports["pe"],
        "combined_test": _aggregate_metric_rows(combined_fold_rows),
        "trading_utility": utility_summary,
    }


def _objective_value(experiment_result: Dict[str, object], objective: str) -> Optional[float]:
    obj = str(objective).strip().lower()
    if obj in {"rmse", "brier"}:
        return experiment_result["combined_test"].get(f"{obj}_mean")
    if obj in {"f1", "roc_auc", "pr_auc", "accuracy", "precision", "recall"}:
        ce_val = experiment_result["ce"]["aggregate"]["test"].get(f"{obj}_mean")
        pe_val = experiment_result["pe"]["aggregate"]["test"].get(f"{obj}_mean")
        vals = [v for v in (ce_val, pe_val) if v is not None]
        if not vals:
            return None
        return float(np.mean(vals))
    if obj == "trade_utility":
        utility = experiment_result.get("trading_utility") or {}
        if not bool(utility.get("constraints_pass", False)):
            return None
        val = _safe_float(utility.get("net_return_sum"))
        return float(val) if np.isfinite(val) else None
    raise ValueError(f"unsupported objective: {objective}")


def _fallback_objective_value(experiment_result: Dict[str, object], objective: str) -> Optional[float]:
    obj = str(objective).strip().lower()
    if obj == "trade_utility":
        utility = experiment_result.get("trading_utility") or {}
        val = _safe_float(utility.get("net_return_sum"))
        return float(val) if np.isfinite(val) else None
    return _objective_value(experiment_result, objective)


def _is_better(candidate: Dict[str, object], incumbent: Dict[str, object], objective: str) -> bool:
    minimize = str(objective).strip().lower() in {"rmse", "brier"}
    c = candidate.get("objective_value")
    i = incumbent.get("objective_value")
    if c is None:
        return False
    if i is None:
        return True
    if minimize:
        if c < i:
            return True
        if c > i:
            return False
    else:
        if c > i:
            return True
        if c < i:
            return False
    # tie-break: fewer features, then name order for determinism
    cf = int(candidate.get("feature_count", 10**9))
    inf = int(incumbent.get("feature_count", 10**9))
    if cf != inf:
        return cf < inf
    return str(candidate.get("experiment_id", "")) < str(incumbent.get("experiment_id", ""))


def _fit_final_models(
    labeled_df: pd.DataFrame,
    feature_columns: Sequence[str],
    model_spec: ModelSpec,
    random_state: int,
    preprocess_cfg: PreprocessConfig,
    label_target: str,
) -> Dict[str, object]:
    models: Dict[str, object] = {}
    for side in ("ce", "pe"):
        side_df = _prepare_side_df(labeled_df, side, label_target=label_target)
        y = side_df["target"].astype(int).to_numpy()
        classes = np.unique(y)
        if len(classes) < 2:
            model = ConstantProbModel(float(classes[0]) if len(classes) == 1 else 0.0)
        else:
            model = _build_model(model_spec, random_state=random_state, preprocess_cfg=preprocess_cfg)
            model.fit(side_df.loc[:, list(feature_columns)], y)
        models[side] = model
    return models


def run_training_cycle(
    labeled_df: pd.DataFrame,
    feature_profile: str = FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    objective: str = "rmse",
    train_days: int = 3,
    valid_days: int = 1,
    test_days: int = 1,
    step_days: int = 1,
    purge_days: int = 0,
    embargo_days: int = 0,
    random_state: int = 42,
    max_experiments: Optional[int] = None,
    preprocess_cfg: Optional[PreprocessConfig] = None,
    label_target: str = LABEL_TARGET_BASE,
    utility_cfg: Optional[TradingObjectiveConfig] = None,
) -> Dict[str, object]:
    frame = _ensure_sorted(labeled_df)
    normalized_label_target = str(label_target).strip().lower()
    if normalized_label_target not in LABEL_TARGET_CHOICES:
        raise ValueError(f"unsupported label_target: {label_target}")
    effective_preprocess = preprocess_cfg or PreprocessConfig()
    effective_utility = utility_cfg or TradingObjectiveConfig()
    base_features = select_feature_columns(frame, feature_profile=feature_profile)
    if not base_features:
        raise ValueError("no base features for training cycle")
    base_features, dropped_by_missing = _filter_features_by_missing_rate(
        frame,
        columns=base_features,
        max_missing_rate=float(effective_preprocess.max_missing_rate),
    )
    if not base_features:
        raise ValueError("all features dropped by preprocessing missing-rate gate")

    feature_specs = _feature_specs_for_profile(feature_profile=feature_profile)
    model_specs = _default_model_specs()
    experiments: List[Dict[str, object]] = []
    rank_list: List[Dict[str, object]] = []

    for fs in feature_specs:
        selected_features = _apply_feature_set(base_features, fs)
        if not selected_features:
            continue
        for ms in model_specs:
            exp_id = f"{fs.name}__{ms.name}"
            result = _evaluate_experiment(
                df=frame,
                feature_columns=selected_features,
                model_spec=ms,
                train_days=train_days,
                valid_days=valid_days,
                test_days=test_days,
                step_days=step_days,
                purge_days=purge_days,
                embargo_days=embargo_days,
                random_state=random_state,
                preprocess_cfg=effective_preprocess,
                label_target=normalized_label_target,
                utility_cfg=effective_utility,
            )
            objective_value = _objective_value(result, objective=objective)
            entry = {
                "experiment_id": exp_id,
                "feature_set": fs.name,
                "model": {"name": ms.name, "family": ms.family, "params": ms.params},
                "feature_count": int(len(selected_features)),
                "feature_columns": selected_features,
                "objective": str(objective),
                "objective_value": objective_value,
                "result": result,
            }
            experiments.append(entry)
            rank_list.append(
                {
                    "experiment_id": exp_id,
                    "feature_set": fs.name,
                    "model_name": ms.name,
                    "model_family": ms.family,
                    "feature_count": int(len(selected_features)),
                    "objective": str(objective),
                    "objective_value": objective_value,
                    "ce_test_rmse_mean": result["ce"]["aggregate"]["test"].get("rmse_mean"),
                    "pe_test_rmse_mean": result["pe"]["aggregate"]["test"].get("rmse_mean"),
                    "ce_test_f1_mean": result["ce"]["aggregate"]["test"].get("f1_mean"),
                    "pe_test_f1_mean": result["pe"]["aggregate"]["test"].get("f1_mean"),
                    "utility_net_return_sum": result["trading_utility"].get("net_return_sum"),
                    "utility_profit_factor": result["trading_utility"].get("profit_factor"),
                    "utility_max_drawdown_r": result["trading_utility"].get("max_drawdown_r"),
                    "utility_max_drawdown_pct": result["trading_utility"].get("max_drawdown_pct"),
                    "utility_trades_total": result["trading_utility"].get("trades_total"),
                    "utility_constraints_pass": result["trading_utility"].get("constraints_pass"),
                }
            )
            if max_experiments is not None and len(experiments) >= int(max_experiments):
                break
        if max_experiments is not None and len(experiments) >= int(max_experiments):
            break

    if not experiments:
        raise ValueError("no experiments were executed")

    promotable = [exp for exp in experiments if exp.get("objective_value") is not None]
    no_promotable_model = len(promotable) == 0
    if promotable:
        best = promotable[0]
        for exp in promotable[1:]:
            if _is_better(exp, best, objective=objective):
                best = exp
    else:
        fallback = []
        for exp in experiments:
            candidate = dict(exp)
            candidate["objective_value"] = _fallback_objective_value(exp["result"], objective=objective)
            fallback.append(candidate)
        best = fallback[0]
        for exp in fallback[1:]:
            if _is_better(exp, best, objective=objective):
                best = exp
        # keep entry in canonical schema with explicit fallback marker
        best = {
            **best,
            "objective_value": None,
            "fallback_objective_value": _fallback_objective_value(best["result"], objective=objective),
            "selected_by_fallback": True,
        }

    best_model_spec = ModelSpec(
        name=str(best["model"]["name"]),
        family=str(best["model"]["family"]),
        params=dict(best["model"]["params"]),
    )
    best_models = _fit_final_models(
        labeled_df=frame,
        feature_columns=list(best["feature_columns"]),
        model_spec=best_model_spec,
        random_state=random_state,
        preprocess_cfg=effective_preprocess,
        label_target=normalized_label_target,
    )

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": int(len(frame)),
        "days_total": int(frame["trade_date"].nunique()),
        "feature_profile": str(feature_profile),
        "objective": str(objective),
        "label_target": normalized_label_target,
        "cv_config": {
            "train_days": int(train_days),
            "valid_days": int(valid_days),
            "test_days": int(test_days),
            "step_days": int(step_days),
            "purge_days": int(purge_days),
            "embargo_days": int(embargo_days),
        },
        "preprocessing": {
            "max_missing_rate": float(effective_preprocess.max_missing_rate),
            "clip_lower_q": float(effective_preprocess.clip_lower_q),
            "clip_upper_q": float(effective_preprocess.clip_upper_q),
            "dropped_features_by_missing_rate": dropped_by_missing,
            "features_after_preprocess_gate": int(len(base_features)),
        },
        "trading_utility_config": {
            "ce_threshold": float(effective_utility.ce_threshold),
            "pe_threshold": float(effective_utility.pe_threshold),
            "cost_per_trade": float(effective_utility.cost_per_trade),
            "min_profit_factor": float(effective_utility.min_profit_factor),
            "max_equity_drawdown_pct": float(effective_utility.max_equity_drawdown_pct),
            "min_trades": int(effective_utility.min_trades),
            "take_profit_pct": float(effective_utility.take_profit_pct),
            "stop_loss_pct": float(effective_utility.stop_loss_pct),
            "discard_time_stop": bool(effective_utility.discard_time_stop),
            "risk_per_trade_pct": float(effective_utility.risk_per_trade_pct),
        },
        "promotion": {
            "promotable_count": int(len(promotable)),
            "no_promotable_model": bool(no_promotable_model),
        },
        "search_space": {
            "feature_sets": [fs.name for fs in feature_specs],
            "models": [{"name": ms.name, "family": ms.family, "params": ms.params} for ms in model_specs],
        },
        "experiments_total": int(len(experiments)),
        "leaderboard": sorted(
            rank_list,
            key=lambda x: (
                float("inf") if x["objective_value"] is None else float(x["objective_value"]),
                int(x["feature_count"]),
                str(x["experiment_id"]),
            )
            if str(objective).lower() in {"rmse", "brier"}
            else (
                -(float("-inf") if x["objective_value"] is None else float(x["objective_value"])),
                int(x["feature_count"]),
                str(x["experiment_id"]),
            ),
        ),
        "best_experiment": best,
    }

    model_package = {
        "kind": "t29_training_cycle_model_package",
        "created_at_utc": report["created_at_utc"],
        "feature_profile": str(feature_profile),
        "objective": str(objective),
        "label_target": normalized_label_target,
        "feature_columns": list(best["feature_columns"]),
        "selected_feature_set": str(best["feature_set"]),
        "selected_model": dict(best["model"]),
        "cv_config": dict(report["cv_config"]),
        "preprocessing": dict(report["preprocessing"]),
        "trading_utility_config": dict(report["trading_utility_config"]),
        "models": best_models,
    }
    return {"report": report, "model_package": model_package}


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Iterative model training cycle (feature/model search)")
    parser.add_argument("--labeled-data", default="ml_pipeline/artifacts/t05_labeled_features.parquet")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t29_training_cycle_report.json")
    parser.add_argument("--model-out", default="ml_pipeline/artifacts/t29_best_model.joblib")
    parser.add_argument("--feature-profile", default=FEATURE_PROFILE_FUTURES_OPTIONS_ONLY, choices=list(FEATURE_PROFILES))
    parser.add_argument(
        "--objective",
        default="rmse",
        choices=["rmse", "brier", "f1", "roc_auc", "pr_auc", "accuracy", "precision", "recall", "trade_utility"],
    )
    parser.add_argument("--label-target", default=LABEL_TARGET_BASE, choices=list(LABEL_TARGET_CHOICES))
    parser.add_argument("--train-days", type=int, default=3)
    parser.add_argument("--valid-days", type=int, default=1)
    parser.add_argument("--test-days", type=int, default=1)
    parser.add_argument("--step-days", type=int, default=1)
    parser.add_argument("--purge-days", type=int, default=0)
    parser.add_argument("--embargo-days", type=int, default=0)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--max-experiments", type=int, default=None)
    parser.add_argument("--max-missing-rate", type=float, default=0.35)
    parser.add_argument("--clip-lower-q", type=float, default=0.01)
    parser.add_argument("--clip-upper-q", type=float, default=0.99)
    parser.add_argument("--utility-ce-threshold", type=float, default=0.60)
    parser.add_argument("--utility-pe-threshold", type=float, default=0.60)
    parser.add_argument("--utility-cost-per-trade", type=float, default=0.0006)
    parser.add_argument("--utility-min-profit-factor", type=float, default=1.30)
    parser.add_argument(
        "--utility-max-equity-drawdown-pct",
        type=float,
        default=0.15,
        help="Absolute equity drawdown limit (fraction, 0.15 = 15 percent).",
    )
    parser.add_argument("--utility-max-abs-drawdown", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--utility-min-trades", type=int, default=50)
    parser.add_argument(
        "--utility-risk-per-trade-pct",
        type=float,
        default=0.01,
        help="Capital risk per trade as fraction (0.01 = 1 percent).",
    )
    parser.add_argument("--utility-take-profit-pct", type=float, default=0.30)
    parser.add_argument("--utility-stop-loss-pct", type=float, default=0.20)
    parser.add_argument("--utility-keep-time-stop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--utility-discard-time-stop",
        action="store_true",
        help="Discard time-stop rows in utility simulation (default includes them).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    if not labeled_path.exists():
        print(f"ERROR: labeled dataset not found: {labeled_path}")
        return 2
    df = pd.read_parquet(labeled_path)
    out = run_training_cycle(
        labeled_df=df,
        feature_profile=str(args.feature_profile),
        objective=str(args.objective),
        label_target=str(args.label_target),
        train_days=int(args.train_days),
        valid_days=int(args.valid_days),
        test_days=int(args.test_days),
        step_days=int(args.step_days),
        purge_days=int(args.purge_days),
        embargo_days=int(args.embargo_days),
        random_state=int(args.random_state),
        max_experiments=args.max_experiments,
        preprocess_cfg=PreprocessConfig(
            max_missing_rate=float(args.max_missing_rate),
            clip_lower_q=float(args.clip_lower_q),
            clip_upper_q=float(args.clip_upper_q),
        ),
        utility_cfg=TradingObjectiveConfig(
            ce_threshold=float(args.utility_ce_threshold),
            pe_threshold=float(args.utility_pe_threshold),
            cost_per_trade=float(args.utility_cost_per_trade),
            min_profit_factor=float(args.utility_min_profit_factor),
            max_equity_drawdown_pct=(
                float(args.utility_max_abs_drawdown)
                if args.utility_max_abs_drawdown is not None
                else float(args.utility_max_equity_drawdown_pct)
            ),
            min_trades=int(args.utility_min_trades),
            take_profit_pct=float(args.utility_take_profit_pct),
            stop_loss_pct=float(args.utility_stop_loss_pct),
            discard_time_stop=bool(args.utility_discard_time_stop) and (not bool(args.utility_keep_time_stop)),
            risk_per_trade_pct=float(args.utility_risk_per_trade_pct),
        ),
    )

    report_out = Path(args.report_out)
    model_out = Path(args.model_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    model_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(out["report"], indent=2), encoding="utf-8")
    joblib.dump(out["model_package"], model_out)

    best = out["report"]["best_experiment"]
    print(f"Rows: {out['report']['rows_total']}")
    print(f"Experiments: {out['report']['experiments_total']}")
    print(f"Objective: {out['report']['objective']}")
    print(f"Best: {best['experiment_id']}")
    print(f"Best objective value: {best['objective_value']}")
    print(f"Best feature count: {best['feature_count']}")
    print(f"Report: {report_out}")
    print(f"Model: {model_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
