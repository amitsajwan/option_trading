from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ..contracts.types import PreprocessConfig, RecoveryRecipe, TradingObjectiveConfig
from ..dataset_windowing import filter_trade_dates, load_feature_frame
from ..evaluation import FuturesPromotionGates, evaluate_futures_stages_from_frame, stage_b
from ..experiment_control.state import RunContext, utc_now
from ..inference_contract import predict_probabilities_from_frame
from ..labeling import EffectiveLabelConfig, build_label_lineage, build_labeled_dataset, prepare_snapshot_labeled_frame
from ..labeling.event_sampling import CusumSamplingConfig, annotate_cusum_events
from ..model_search import run_training_cycle_catalog


META_FEATURE_COLUMNS = (
    "primary_chosen_prob",
    "primary_ce_prob",
    "primary_pe_prob",
    "primary_prob_gap",
    "dealer_proxy_oi_imbalance",
    "dealer_proxy_oi_imbalance_change_5m",
    "dealer_proxy_pcr_change_5m",
    "dealer_proxy_atm_oi_velocity_5m",
    "dealer_proxy_volume_imbalance",
    "ctx_dte_days",
    "ctx_is_high_vix_day",
    "time_minute_of_day",
)

CANDIDATE_FILTER_FIELDS = (
    "require_event_sampled",
    "exclude_expiry_day",
    "exclude_regime_atr_high",
    "require_tradeable_context",
    "allow_near_expiry_context",
)


class _ConstantMetaModel:
    def __init__(self, p1: float):
        self.p1 = float(p1)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        p1 = np.full(int(len(x)), self.p1, dtype=float)
        return np.column_stack([1.0 - p1, p1])


def _normalize_recipe_selection(recipe_ids: Optional[Sequence[str]]) -> Optional[Set[str]]:
    if recipe_ids is None:
        return None
    out: Set[str] = set()
    for value in recipe_ids:
        for token in str(value).split(","):
            recipe_id = str(token).strip()
            if recipe_id:
                out.add(recipe_id)
    return out or None


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except Exception:
        return float("nan")


def _side_penalty(long_share: float) -> float:
    return abs(float(long_share) - 0.5)


def _side_share_in_band(long_share: float) -> bool:
    return 0.35 <= float(long_share) <= 0.65


def _trade_side(ce_prob: float, pe_prob: float, threshold: float) -> Optional[str]:
    ce_ok = float(ce_prob) >= float(threshold)
    pe_ok = float(pe_prob) >= float(threshold)
    if ce_ok and pe_ok:
        return "CE" if float(ce_prob) >= float(pe_prob) else "PE"
    if ce_ok:
        return "CE"
    if pe_ok:
        return "PE"
    return None


def _profit_factor(net_returns: Sequence[float]) -> float:
    gains = float(sum(x for x in net_returns if x > 0.0))
    losses = float(-sum(x for x in net_returns if x < 0.0))
    if losses <= 0.0:
        return 999.0 if gains > 0.0 else 0.0
    return float(gains / losses)


def _path_reason_return(row: pd.Series, side: str) -> Optional[float]:
    prefix = "ce" if str(side).upper() == "CE" else "pe"
    realized = _safe_float(row.get(f"{prefix}_realized_return"))
    reason = str(row.get(f"{prefix}_path_exit_reason", "")).strip().lower()
    if np.isfinite(realized):
        return float(realized)
    if reason in {"tp", "tp_sl_same_bar"}:
        return float(_safe_float(row.get(f"{prefix}_barrier_upper_return")))
    if reason == "sl":
        return -float(_safe_float(row.get(f"{prefix}_barrier_lower_return")))
    fr = _safe_float(row.get(f"{prefix}_forward_return"))
    return float(fr) if np.isfinite(fr) else None


def _compare_to_phase2_baseline(candidate: Dict[str, Any], baseline_payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(baseline_payload, dict):
        return None
    baseline = dict((baseline_payload.get("winner") or {}).get("candidate") or {})
    if not baseline:
        return None
    candidate_pf = float(candidate.get("profit_factor", 0.0))
    baseline_pf = float(baseline.get("profit_factor", 0.0))
    candidate_net = float(candidate.get("net_return_sum", 0.0))
    baseline_net = float(baseline.get("net_return_sum", 0.0))
    return {
        "baseline_recipe_id": ((baseline_payload.get("winner") or {}).get("recipe") or {}).get("recipe_id"),
        "profit_factor_delta": candidate_pf - baseline_pf,
        "net_return_sum_delta": candidate_net - baseline_net,
        "trade_delta": int(candidate.get("trades", 0)) - int(baseline.get("trades", 0)),
        "side_penalty_delta": float(candidate.get("side_penalty", 0.0)) - float(baseline.get("side_penalty", 0.0)),
        "advance_candidate": bool(bool(candidate.get("stage_a_passed")) and _side_share_in_band(float(candidate.get("long_share", 0.0))) and candidate_net > baseline_net and (candidate_pf - baseline_pf) >= -0.10),
    }


def _atr_barrier_multipliers(features: pd.DataFrame, *, take_profit_pct: float, stop_loss_pct: float, atr_reference_col: str = "osc_atr_ratio") -> tuple[float, float]:
    atr_ref = pd.to_numeric(features.get(atr_reference_col, features.get("atr_ratio")), errors="coerce")
    atr_ref = atr_ref.replace([np.inf, -np.inf], np.nan)
    atr_ref = atr_ref[atr_ref > 0.0]
    median_ref = float(atr_ref.median()) if len(atr_ref) else float("nan")
    if not np.isfinite(median_ref) or median_ref <= 0.0:
        return 1.0, 1.0
    return float(take_profit_pct) / median_ref, float(stop_loss_pct) / median_ref


def _preprocess_cfg(payload: Dict[str, Any]) -> PreprocessConfig:
    return PreprocessConfig(max_missing_rate=float(payload.get("max_missing_rate", 0.35)), clip_lower_q=float(payload.get("clip_lower_q", 0.01)), clip_upper_q=float(payload.get("clip_upper_q", 0.99)))


def _utility_cfg(payload: Dict[str, Any]) -> TradingObjectiveConfig:
    return TradingObjectiveConfig(ce_threshold=float(payload.get("ce_threshold", 0.25)), pe_threshold=float(payload.get("pe_threshold", 0.25)), cost_per_trade=float(payload.get("cost_per_trade", 0.0006)), min_profit_factor=float(payload.get("min_profit_factor", 1.1)), max_equity_drawdown_pct=float(payload.get("max_equity_drawdown_pct", 0.2)), min_trades=int(payload.get("min_trades", 25)), take_profit_pct=float(payload.get("take_profit_pct", 0.0025)), stop_loss_pct=float(payload.get("stop_loss_pct", 0.0010)), discard_time_stop=bool(payload.get("discard_time_stop", False)), risk_per_trade_pct=float(payload.get("risk_per_trade_pct", 0.01)))


def _gates(payload: Dict[str, Any]) -> FuturesPromotionGates:
    merged = FuturesPromotionGates().__dict__ | dict(payload or {})
    return FuturesPromotionGates(**merged)


def _effective_label_cfg(recipe: RecoveryRecipe, *, train_features: pd.DataFrame, event_sampling_mode: str, event_signal_col: Optional[str]) -> EffectiveLabelConfig:
    atr_tp_multiplier, atr_sl_multiplier = _atr_barrier_multipliers(train_features, take_profit_pct=float(recipe.take_profit_pct), stop_loss_pct=float(recipe.stop_loss_pct))
    return EffectiveLabelConfig(horizon_minutes=int(recipe.horizon_minutes), return_threshold=0.0, use_excursion_gate=False, min_favorable_excursion=0.0, max_adverse_excursion=0.0, stop_loss_pct=float(recipe.stop_loss_pct), take_profit_pct=float(recipe.take_profit_pct), allow_hold_extension=False, extension_trigger_profit_pct=0.0, barrier_mode=str(recipe.barrier_mode), atr_reference_col="osc_atr_ratio", atr_tp_multiplier=float(atr_tp_multiplier), atr_sl_multiplier=float(atr_sl_multiplier), atr_clip_min_factor=0.50, atr_clip_max_factor=1.50, neutral_policy="exclude_from_primary", event_sampling_mode=str(event_sampling_mode), event_signal_col=(str(event_signal_col) if event_signal_col else None), event_end_ts_mode="first_touch_or_vertical")


def _prepare_labeled_frame(features: pd.DataFrame, *, recipe: RecoveryRecipe, label_cfg: EffectiveLabelConfig, event_sampling_mode: str, context: str) -> tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any]]:
    work = features.copy()
    sampling_meta: Dict[str, Any] = {"event_sampling_mode": "none", "event_signal_col": None, "event_rows": int(len(work)), "rows_total": int(len(work))}
    if str(event_sampling_mode).strip().lower() == "cusum":
        work, sampling_meta = annotate_cusum_events(work, config=CusumSamplingConfig())
    elif "event_sampled" not in work.columns:
        work["event_sampled"] = 1.0
        work["event_sample_direction"] = 0.0
    labeled = build_labeled_dataset(features=work, cfg=label_cfg)
    labeled = prepare_snapshot_labeled_frame(labeled, context=context)
    if str(event_sampling_mode).strip().lower() == "cusum":
        labeled = labeled[pd.to_numeric(labeled.get("event_sampled"), errors="coerce").fillna(0.0) == 1.0].copy().reset_index(drop=True)
    return labeled, sampling_meta, build_label_lineage(labeled, label_cfg)


def _normalize_candidate_filter(payload: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    raw = dict(payload or {})
    return {
        field: (raw.get(field) if isinstance(raw.get(field), bool) else False)
        for field in CANDIDATE_FILTER_FIELDS
    }


def _first_available_flag(frame: pd.DataFrame, columns: Sequence[str]) -> pd.Series:
    present = [
        pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        for column in columns
        if column in frame.columns
    ]
    if not present:
        return pd.Series(0.0, index=frame.index, dtype=float)
    out = present[0].astype(float).copy()
    for series in present[1:]:
        out = pd.Series(np.maximum(out.to_numpy(dtype=float), series.to_numpy(dtype=float)), index=frame.index, dtype=float)
    return out.fillna(0.0)


def apply_candidate_filter(
    frame: pd.DataFrame,
    *,
    candidate_filter: Optional[Dict[str, Any]],
    context: str,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    config = _normalize_candidate_filter(candidate_filter)
    filtered = frame.copy()
    dropped_by_rule: Dict[str, int] = {}

    def _apply_rule(rule_name: str, keep_mask: pd.Series) -> None:
        nonlocal filtered
        before = int(len(filtered))
        filtered = filtered.loc[keep_mask.fillna(False)].copy().reset_index(drop=True)
        dropped_by_rule[rule_name] = before - int(len(filtered))

    if config["require_event_sampled"]:
        sampled = pd.to_numeric(filtered.get("event_sampled"), errors="coerce").fillna(0.0) == 1.0
        _apply_rule("require_event_sampled", sampled)
    else:
        dropped_by_rule["require_event_sampled"] = 0

    if config["exclude_expiry_day"]:
        expiry_day = _first_available_flag(filtered, ("ctx_is_expiry_day",)) == 1.0
        _apply_rule("exclude_expiry_day", ~expiry_day)
    else:
        dropped_by_rule["exclude_expiry_day"] = 0

    if config["exclude_regime_atr_high"]:
        atr_high = _first_available_flag(filtered, ("ctx_regime_atr_high", "regime_atr_high")) == 1.0
        _apply_rule("exclude_regime_atr_high", ~atr_high)
    else:
        dropped_by_rule["exclude_regime_atr_high"] = 0

    if config["require_tradeable_context"]:
        tradeable = _first_available_flag(
            filtered,
            ("ctx_regime_trend_up", "ctx_regime_trend_down", "regime_trend_up", "regime_trend_down"),
        ) == 1.0
        if config["allow_near_expiry_context"]:
            tradeable = tradeable | (
                _first_available_flag(filtered, ("ctx_regime_expiry_near", "regime_expiry_near")) == 1.0
            )
        _apply_rule("require_tradeable_context", tradeable)
    else:
        dropped_by_rule["require_tradeable_context"] = 0

    meta = {
        "context": str(context),
        "candidate_filter": config,
        "rows_before": int(len(frame)),
        "rows_after": int(len(filtered)),
        "dropped_by_rule": dropped_by_rule,
    }
    if len(filtered) <= 0:
        raise ValueError(f"{context} candidate_filter removed all rows")
    return filtered, meta


def _holdout_candidate_summary(holdout_labeled: pd.DataFrame, model_package: Dict[str, Any], *, threshold: float, gates: FuturesPromotionGates, cost_per_trade: float) -> Dict[str, Any]:
    probs, input_contract = predict_probabilities_from_frame(holdout_labeled, model_package, missing_policy_override="error", context="recovery.primary_holdout")
    stage_eval = evaluate_futures_stages_from_frame(frame=holdout_labeled, probs=probs, ce_threshold=float(threshold), pe_threshold=float(threshold), cost_per_trade=float(cost_per_trade), gates=gates)
    raw_stage_b = stage_b(frame=holdout_labeled, probs=probs, ce_threshold=float(threshold), pe_threshold=float(threshold), cost_per_trade=float(cost_per_trade), gates=gates)
    long_share = float(raw_stage_b.get("long_share", 0.0))
    return {
        "input_contract": input_contract,
        "stage_eval": stage_eval,
        "stage_a_passed": bool(((stage_eval.get("stage_a_predictive_quality") or {}).get("passed"))),
        "trades": int(raw_stage_b.get("trades", 0)),
        "long_trades": int(raw_stage_b.get("long_trades", 0)),
        "short_trades": int(raw_stage_b.get("short_trades", 0)),
        "hold_count": int(raw_stage_b.get("hold_count", 0)),
        "long_share": long_share,
        "short_share": float(raw_stage_b.get("short_share", 0.0)),
        "side_share_in_band": _side_share_in_band(long_share),
        "side_penalty": _side_penalty(long_share),
        "profit_factor": float(raw_stage_b.get("profit_factor", 0.0)),
        "gross_profit_factor": float(raw_stage_b.get("gross_profit_factor", 0.0)),
        "net_return_sum": float(raw_stage_b.get("net_return_sum", 0.0)),
        "gross_return_sum": float(raw_stage_b.get("gross_return_sum", 0.0)),
        "mean_net_return_per_trade": float(raw_stage_b.get("mean_net_return_per_trade", 0.0)),
        "mean_gross_return_per_trade": float(raw_stage_b.get("mean_gross_return_per_trade", 0.0)),
        "win_rate": float(raw_stage_b.get("win_rate", 0.0)),
        "gross_win_rate": float(raw_stage_b.get("gross_win_rate", 0.0)),
        "tp_trades": int(raw_stage_b.get("tp_trades", 0)),
        "sl_trades": int(raw_stage_b.get("sl_trades", 0)),
        "time_stop_trades": int(raw_stage_b.get("time_stop_trades", 0)),
        "tp_sl_same_bar_trades": int(raw_stage_b.get("tp_sl_same_bar_trades", 0)),
        "invalid_trades": int(raw_stage_b.get("invalid_trades", 0)),
        "time_stop_gross_wins": int(raw_stage_b.get("time_stop_gross_wins", 0)),
        "time_stop_gross_losses": int(raw_stage_b.get("time_stop_gross_losses", 0)),
        "time_stop_net_wins": int(raw_stage_b.get("time_stop_net_wins", 0)),
        "time_stop_net_losses": int(raw_stage_b.get("time_stop_net_losses", 0)),
        "positive_net_return": bool(float(raw_stage_b.get("net_return_sum", 0.0)) > 0.0),
        "positive_gross_return": bool(float(raw_stage_b.get("gross_return_sum", 0.0)) > 0.0),
        "block_rate": float(raw_stage_b.get("block_rate", 0.0)),
        "rows_total": int(raw_stage_b.get("rows_total", 0)),
    }


def _build_meta_candidate_dataset(*, labeled_df: pd.DataFrame, utility_payload: Dict[str, object], threshold: float, cost_per_trade: float) -> pd.DataFrame:
    folds = list(utility_payload.get("folds") or [])
    ce_scores = dict(utility_payload.get("ce_scores") or {})
    pe_scores = dict(utility_payload.get("pe_scores") or {})
    rows: List[Dict[str, Any]] = []
    feature_cols = [col for col in META_FEATURE_COLUMNS if col in labeled_df.columns]
    keep_cols = [col for col in ("timestamp", "trade_date", "dealer_proxy_oi_imbalance", "dealer_proxy_oi_imbalance_change_5m", "dealer_proxy_pcr_change_5m", "dealer_proxy_atm_oi_velocity_5m", "dealer_proxy_volume_imbalance", "ctx_dte_days", "ctx_is_high_vix_day", "time_minute_of_day", "ce_path_exit_reason", "pe_path_exit_reason", "ce_forward_return", "pe_forward_return", "ce_barrier_upper_return", "ce_barrier_lower_return", "pe_barrier_upper_return", "pe_barrier_lower_return", "ce_realized_return", "pe_realized_return") if col in labeled_df.columns]
    for fold_idx, fold in enumerate(folds, start=1):
        ce_df = ce_scores.get(fold_idx)
        pe_df = pe_scores.get(fold_idx)
        if ce_df is None or pe_df is None:
            continue
        actual = labeled_df.copy()
        actual["trade_date"] = pd.to_datetime(actual["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        actual = actual[actual["trade_date"].isin(list(fold.get("test_days") or []))].copy()
        if len(actual) == 0:
            continue
        merged = actual.loc[:, keep_cols].merge(ce_df, on=["timestamp", "trade_date"], how="inner").merge(pe_df, on=["timestamp", "trade_date"], how="inner")
        for row in merged.itertuples(index=False):
            payload = pd.Series(row._asdict())
            ce_prob = _safe_float(payload.get("ce_prob"))
            pe_prob = _safe_float(payload.get("pe_prob"))
            chosen_side = _trade_side(ce_prob, pe_prob, threshold=float(threshold))
            if chosen_side is None:
                continue
            realized_gross = _path_reason_return(payload, side=chosen_side)
            if realized_gross is None:
                continue
            row_out = {"timestamp": payload.get("timestamp"), "trade_date": payload.get("trade_date"), "chosen_side": chosen_side, "realized_net_return_after_cost": float(realized_gross - float(cost_per_trade)), "meta_target": int((float(realized_gross) - float(cost_per_trade)) > 0.0), "primary_chosen_prob": float(ce_prob if chosen_side == "CE" else pe_prob), "primary_ce_prob": float(ce_prob), "primary_pe_prob": float(pe_prob), "primary_prob_gap": float(abs(ce_prob - pe_prob))}
            for feature_col in feature_cols:
                row_out[feature_col] = _safe_float(payload.get(feature_col))
            rows.append(row_out)
    out = pd.DataFrame(rows)
    if len(out) == 0:
        return out
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values(["timestamp"]).reset_index(drop=True)


def _meta_trade_summary(frame: pd.DataFrame, *, threshold: float) -> Dict[str, Any]:
    if len(frame) == 0:
        return {"threshold": float(threshold), "trades": 0, "net_return_sum": 0.0, "profit_factor": 0.0, "win_rate": 0.0, "ce_share": 0.0}
    taken = frame[pd.to_numeric(frame["meta_prob"], errors="coerce").fillna(0.0) >= float(threshold)].copy()
    if len(taken) == 0:
        return {"threshold": float(threshold), "trades": 0, "net_return_sum": 0.0, "profit_factor": 0.0, "win_rate": 0.0, "ce_share": 0.0}
    net_returns = pd.to_numeric(taken["realized_net_return_after_cost"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    ce_share = float(np.mean(taken["chosen_side"].astype(str).str.upper() == "CE")) if len(taken) else 0.0
    return {"threshold": float(threshold), "trades": int(len(taken)), "net_return_sum": float(np.sum(net_returns)), "profit_factor": float(_profit_factor(net_returns)), "win_rate": float(np.mean(net_returns > 0.0)) if len(net_returns) else 0.0, "ce_share": ce_share}


def _fit_meta_model(meta_candidates: pd.DataFrame, *, threshold_grid: Sequence[float]) -> Tuple[Pipeline, float, Dict[str, Any]]:
    if len(meta_candidates) == 0:
        raise ValueError("meta candidate dataset is empty")
    candidate_days = sorted(meta_candidates["trade_date"].astype(str).unique().tolist())
    valid_day_count = 1 if len(candidate_days) < 5 else max(1, int(round(len(candidate_days) * 0.20)))
    train_days = candidate_days[:-valid_day_count] or candidate_days
    valid_days = candidate_days[-valid_day_count:] if len(candidate_days) > len(train_days) else candidate_days[-1:]
    train_df = meta_candidates[meta_candidates["trade_date"].astype(str).isin(train_days)].copy()
    valid_df = meta_candidates[meta_candidates["trade_date"].astype(str).isin(valid_days)].copy()
    x_cols = [col for col in META_FEATURE_COLUMNS if col in meta_candidates.columns]
    y_train = train_df["meta_target"].astype(int).to_numpy()
    if len(pd.Series(y_train).unique()) < 2:
        model = _ConstantMetaModel(float(y_train[0]) if len(y_train) else 0.0)
    else:
        model = Pipeline(steps=[("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler(with_mean=True, with_std=True)), ("model", LogisticRegression(max_iter=1000, random_state=42, class_weight="balanced"))])
        model.fit(train_df.loc[:, x_cols], y_train)
    valid_df = valid_df.copy()
    valid_df["meta_prob"] = model.predict_proba(valid_df.loc[:, x_cols])[:, 1]
    threshold_rows = [_meta_trade_summary(valid_df, threshold=float(thr)) for thr in list(threshold_grid)]
    best = max(threshold_rows, key=lambda row: (float(row.get("net_return_sum", float("-inf"))), float(row.get("profit_factor", float("-inf"))), float(row.get("trades", 0))))
    return model, float(best["threshold"]), {"feature_columns": x_cols, "train_days": train_days, "valid_days": valid_days, "validation_threshold_grid": threshold_rows, "selected_threshold": float(best["threshold"]), "selected_validation_summary": best}


def _build_meta_holdout_dataset(*, holdout_labeled: pd.DataFrame, primary_package: Dict[str, Any], threshold: float, cost_per_trade: float) -> pd.DataFrame:
    probs, _ = predict_probabilities_from_frame(holdout_labeled, primary_package, missing_policy_override="error", context="recovery.meta_holdout")
    payload = holdout_labeled.copy()
    payload["primary_ce_prob"] = pd.to_numeric(probs.get("ce_prob"), errors="coerce")
    payload["primary_pe_prob"] = pd.to_numeric(probs.get("pe_prob"), errors="coerce")
    rows: List[Dict[str, Any]] = []
    feature_cols = [col for col in META_FEATURE_COLUMNS if col in payload.columns]
    for row in payload.itertuples(index=False):
        series = pd.Series(row._asdict())
        ce_prob = _safe_float(series.get("primary_ce_prob"))
        pe_prob = _safe_float(series.get("primary_pe_prob"))
        chosen_side = _trade_side(ce_prob, pe_prob, threshold=float(threshold))
        if chosen_side is None:
            continue
        realized_gross = _path_reason_return(series, side=chosen_side)
        if realized_gross is None:
            continue
        row_out = {"timestamp": series.get("timestamp"), "trade_date": series.get("trade_date"), "chosen_side": chosen_side, "realized_net_return_after_cost": float(realized_gross - float(cost_per_trade)), "primary_chosen_prob": float(ce_prob if chosen_side == "CE" else pe_prob), "primary_ce_prob": float(ce_prob), "primary_pe_prob": float(pe_prob), "primary_prob_gap": float(abs(ce_prob - pe_prob))}
        for feature_col in feature_cols:
            row_out[feature_col] = _safe_float(series.get(feature_col))
        rows.append(row_out)
    out = pd.DataFrame(rows)
    if len(out) == 0:
        return out
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values(["timestamp"]).reset_index(drop=True)


def run_recovery_research(ctx: RunContext) -> Dict[str, Any]:
    resolved = ctx.resolved_config
    inputs = resolved["inputs"]
    windows = resolved["windows"]
    training = resolved["training"]
    scenario = resolved["scenario"]
    baseline_payload = None
    baseline_json = inputs.get("baseline_json_path")
    if baseline_json is not None and Path(baseline_json).exists():
        baseline_payload = json.loads(Path(baseline_json).read_text(encoding="utf-8"))
    selected_recipe_ids = _normalize_recipe_selection(scenario.get("recipe_selection"))
    model_window_features = filter_trade_dates(load_feature_frame(Path(inputs["model_window_features_path"])), windows["full_model"]["start"], windows["full_model"]["end"])
    holdout_features = filter_trade_dates(load_feature_frame(Path(inputs["holdout_features_path"])), windows["final_holdout"]["start"], windows["final_holdout"]["end"])
    preprocess_cfg = _preprocess_cfg(training["preprocess"])
    utility_cfg = _utility_cfg(training["utility"])
    gates = _gates(dict(scenario.get("evaluation_gates") or {}))
    candidate_filter = _normalize_candidate_filter(dict(scenario.get("candidate_filter") or {}))
    primary_results: List[Dict[str, Any]] = []
    selected_primary: Optional[Dict[str, Any]] = None
    for recipe_payload in list(scenario["recipes"]):
        recipe = RecoveryRecipe(**dict(recipe_payload))
        if selected_recipe_ids is not None and recipe.recipe_id not in selected_recipe_ids:
            ctx.append_state("primary_recipe_skipped", recipe_id=recipe.recipe_id, reason="not_selected")
            continue
        recipe_root = ctx.output_root / "primary_recipes" / recipe.recipe_id
        summary_path = recipe_root / "summary.json"
        model_path = recipe_root / "model.joblib"
        training_report_path = recipe_root / "training_report.json"
        if bool(scenario.get("resume_primary")) and summary_path.exists() and model_path.exists() and training_report_path.exists():
            reused = json.loads(summary_path.read_text(encoding="utf-8"))
            ctx.append_state("primary_recipe_skipped", recipe_id=recipe.recipe_id, reason="resume_primary")
            primary_results.append(dict(reused))
            continue
        ctx.append_state("primary_recipe_start", recipe_id=recipe.recipe_id, barrier_mode=recipe.barrier_mode)
        label_cfg = _effective_label_cfg(recipe, train_features=model_window_features, event_sampling_mode=str(scenario.get("event_sampling_mode", "none")), event_signal_col=scenario.get("event_signal_col"))
        train_labeled, train_sampling_meta, train_lineage = _prepare_labeled_frame(model_window_features, recipe=recipe, label_cfg=label_cfg, event_sampling_mode=str(scenario.get("event_sampling_mode", "none")), context=f"recovery:{recipe.recipe_id}:train")
        holdout_labeled, holdout_sampling_meta, holdout_lineage = _prepare_labeled_frame(holdout_features, recipe=recipe, label_cfg=label_cfg, event_sampling_mode=str(scenario.get("event_sampling_mode", "none")), context=f"recovery:{recipe.recipe_id}:holdout")
        train_labeled, train_filtering_meta = apply_candidate_filter(
            train_labeled,
            candidate_filter=candidate_filter,
            context=f"recovery:{recipe.recipe_id}:train",
        )
        holdout_labeled, holdout_filtering_meta = apply_candidate_filter(
            holdout_labeled,
            candidate_filter=candidate_filter,
            context=f"recovery:{recipe.recipe_id}:holdout",
        )
        catalog = run_training_cycle_catalog(labeled_df=train_labeled, feature_profile=str(resolved["catalog"]["feature_profile"]), objective=str(training["objective"]), train_days=int(training["cv_config"]["train_days"]), valid_days=int(training["cv_config"]["valid_days"]), test_days=int(training["cv_config"]["test_days"]), step_days=int(training["cv_config"]["step_days"]), purge_days=int(training["cv_config"].get("purge_days", 0)), embargo_days=int(training["cv_config"].get("embargo_days", 0)), purge_mode=str(training["cv_config"].get("purge_mode", "days")), embargo_rows=int(training["cv_config"].get("embargo_rows", 0)), event_end_col=training["cv_config"].get("event_end_col"), random_state=42, max_experiments=1, preprocess_cfg=preprocess_cfg, label_target=str(training["label_target"]), utility_cfg=utility_cfg, model_whitelist=[str(scenario["primary_model"])], feature_set_whitelist=list(resolved["catalog"]["feature_sets"]), retain_utility_score_payload=True, fit_all_final_models=True, model_n_jobs=int(((training.get("runtime") or {}).get("model_n_jobs")) or 1))
        selected_bundle = list(catalog.get("experiment_bundles") or [])[0]
        model_package = dict(selected_bundle["model_package"])
        holdout_summary = _holdout_candidate_summary(holdout_labeled, model_package, threshold=float(scenario["primary_threshold"]), gates=gates, cost_per_trade=float(utility_cfg.cost_per_trade))
        result = {"recipe": recipe.to_dict(), "label_config": {"barrier_mode": label_cfg.barrier_mode, "atr_reference_col": label_cfg.atr_reference_col, "atr_tp_multiplier": label_cfg.atr_tp_multiplier, "atr_sl_multiplier": label_cfg.atr_sl_multiplier, "neutral_policy": label_cfg.neutral_policy, "event_sampling_mode": label_cfg.event_sampling_mode, "event_signal_col": label_cfg.event_signal_col, "event_end_ts_mode": label_cfg.event_end_ts_mode}, "train_sampling_meta": train_sampling_meta, "holdout_sampling_meta": holdout_sampling_meta, "train_filtering_meta": train_filtering_meta, "holdout_filtering_meta": holdout_filtering_meta, "train_rows": int(len(train_labeled)), "holdout_rows": int(len(holdout_labeled)), "training_report": dict(catalog["report"]), "holdout_summary": holdout_summary, "baseline_comparison": _compare_to_phase2_baseline(holdout_summary, baseline_payload), "model_package_path": str(model_path), "training_report_path": str(training_report_path)}
        recipe_root.mkdir(parents=True, exist_ok=True)
        joblib.dump(model_package, model_path)
        training_report_path.write_text(json.dumps(dict(catalog["report"]), indent=2), encoding="utf-8")
        summary_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        (recipe_root / "train_label_lineage.json").write_text(json.dumps(train_lineage, indent=2), encoding="utf-8")
        (recipe_root / "holdout_label_lineage.json").write_text(json.dumps(holdout_lineage, indent=2), encoding="utf-8")
        ctx.append_state(
            "primary_recipe_done",
            recipe_id=recipe.recipe_id,
            barrier_mode=recipe.barrier_mode,
            stage_a_passed=bool(holdout_summary.get("stage_a_passed")),
            trades=int(holdout_summary.get("trades", 0)),
            profit_factor=float(holdout_summary.get("profit_factor", 0.0)),
            net_return_sum=float(holdout_summary.get("net_return_sum", 0.0)),
        )
        primary_results.append({**result, "selected_bundle": selected_bundle, "model_package": model_package, "train_labeled": train_labeled, "holdout_labeled": holdout_labeled})
    if primary_results:
        selected_primary = max(primary_results, key=lambda row: (float((row["holdout_summary"]).get("stage_a_passed", False)), float((row["holdout_summary"]).get("side_share_in_band", False)), float((row["holdout_summary"]).get("profit_factor", float("-inf"))), float((row["holdout_summary"]).get("net_return_sum", float("-inf")))))
    meta_gate_summary: Optional[Dict[str, Any]] = None
    meta_ready = bool(selected_primary is not None and "selected_bundle" in selected_primary and "model_package" in selected_primary and "train_labeled" in selected_primary and "holdout_labeled" in selected_primary)
    if selected_primary is not None and bool((scenario.get("meta_gate") or {}).get("enabled", False)) and meta_ready:
        meta_root = ctx.output_root / "meta_gate"
        meta_candidates = _build_meta_candidate_dataset(labeled_df=selected_primary["train_labeled"], utility_payload=dict(selected_primary["selected_bundle"].get("utility_score_payload") or {}), threshold=float(scenario["primary_threshold"]), cost_per_trade=float(utility_cfg.cost_per_trade))
        meta_model, meta_threshold, meta_validation = _fit_meta_model(meta_candidates, threshold_grid=list((scenario.get("meta_gate") or {}).get("validation_threshold_grid") or []))
        holdout_candidates = _build_meta_holdout_dataset(holdout_labeled=selected_primary["holdout_labeled"], primary_package=dict(selected_primary["model_package"]), threshold=float(scenario["primary_threshold"]), cost_per_trade=float(utility_cfg.cost_per_trade))
        if len(holdout_candidates) > 0:
            x_cols = [col for col in META_FEATURE_COLUMNS if col in holdout_candidates.columns]
            holdout_candidates = holdout_candidates.copy()
            holdout_candidates["meta_prob"] = meta_model.predict_proba(holdout_candidates.loc[:, x_cols])[:, 1]
        else:
            holdout_candidates["meta_prob"] = []
        holdout_summary = _meta_trade_summary(holdout_candidates, threshold=meta_threshold)
        holdout_summary["stage_a_passed"] = bool(selected_primary["holdout_summary"].get("stage_a_passed"))
        holdout_summary["long_share"] = float(holdout_summary.get("ce_share", 0.0))
        holdout_summary["side_share_in_band"] = _side_share_in_band(float(holdout_summary.get("ce_share", 0.0)))
        holdout_summary["side_penalty"] = _side_penalty(float(holdout_summary.get("ce_share", 0.0)))
        meta_gate_summary = {"created_at_utc": utc_now(), "primary_recipe_id": ((selected_primary.get("recipe") or {}).get("recipe_id")), "primary_threshold": float(scenario["primary_threshold"]), "candidate_rows": int(len(meta_candidates)), "candidate_positive_rate": float(meta_candidates["meta_target"].mean()) if len(meta_candidates) else 0.0, "validation": meta_validation, "holdout_summary": holdout_summary, "baseline_comparison": _compare_to_phase2_baseline(holdout_summary, baseline_payload), "feature_columns": [col for col in META_FEATURE_COLUMNS if col in meta_candidates.columns], "primary_model_package_path": selected_primary["model_package_path"], "meta_model_path": str(meta_root / "meta_model.joblib"), "holdout_candidates_path": str(meta_root / "holdout_candidates.parquet")}
        meta_root.mkdir(parents=True, exist_ok=True)
        joblib.dump({"kind": "fo_expiry_aware_meta_gate_v1", "created_at_utc": utc_now(), "primary_recipe_id": ((selected_primary.get("recipe") or {}).get("recipe_id")), "primary_threshold": float(scenario["primary_threshold"]), "meta_threshold": float(meta_threshold), "feature_columns": meta_gate_summary["feature_columns"], "model": meta_model}, meta_root / "meta_model.joblib")
        holdout_candidates.to_parquet(meta_root / "holdout_candidates.parquet", index=False)
        (meta_root / "summary.json").write_text(json.dumps(meta_gate_summary, indent=2), encoding="utf-8")
    summary = {"created_at_utc": utc_now(), "status": "completed", "paths": {"model_window_features": str(Path(inputs["model_window_features_path"]).resolve()), "holdout_features": str(Path(inputs["holdout_features_path"]).resolve()), "phase2_binary_baseline": (str(Path(baseline_json).resolve()) if baseline_json is not None and Path(baseline_json).exists() else None)}, "event_sampling_mode": str(scenario.get("event_sampling_mode", "none")), "candidate_filter": candidate_filter, "recipe_ids": (sorted(selected_recipe_ids) if selected_recipe_ids else None), "skip_meta": not bool((scenario.get("meta_gate") or {}).get("enabled", False)), "resume_primary": bool(scenario.get("resume_primary", False)), "primary_recipes": [{key: value for key, value in row.items() if key not in {"selected_bundle", "model_package", "train_labeled", "holdout_labeled"}} for row in primary_results], "selected_primary_recipe_id": ((selected_primary.get("recipe") or {}).get("recipe_id") if selected_primary else None), "meta_gate": meta_gate_summary}
    ctx.write_json("summary.json", summary)
    return summary
