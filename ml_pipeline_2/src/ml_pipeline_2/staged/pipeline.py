from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

from ..contracts.types import LabelRecipe, PreprocessConfig
from ..dataset_windowing import filter_trade_dates, normalize_trade_date
from ..evaluation.stage_metrics import calibration_error
from ..experiment_control.state import RunContext, utc_now
from ..inference_contract.predict import predict_probabilities_from_frame
from ..labeling import EffectiveLabelConfig, build_labeled_dataset, prepare_snapshot_labeled_frame
from ..model_search import ensure_requested_models_runnable, run_training_cycle_catalog
from ..model_search.metrics import max_drawdown_pct, profit_factor
from .recipes import get_recipe_catalog
from .registries import resolve_labeler, resolve_policy, resolve_trainer, view_registry

KEY_COLUMNS = ["trade_date", "timestamp", "snapshot_id"]
STAGE_ORDER = ("stage1", "stage2", "stage3")
SUMMARY_SCHEMA_VERSION = 3
STAGE2_SESSION_BUCKETS = ("OPENING", "MORNING", "MIDDAY", "LATE_SESSION")


def _normalize_timestamp_series(values: pd.Series) -> pd.Series:
    out = pd.to_datetime(values, errors="coerce")
    if getattr(out.dt, "tz", None) is not None:
        out = out.dt.tz_localize(None)
    return out


def _load_dataset(parquet_root: Path, dataset_name: str) -> pd.DataFrame:
    dataset_dir = parquet_root / dataset_name
    if not dataset_dir.exists():
        raise FileNotFoundError(f"dataset not found: {dataset_dir}")
    files = sorted(dataset_dir.glob("year=*/data.parquet"))
    if not files:
        files = sorted(dataset_dir.glob("year=*/chunk=*/data.parquet"))
    if not files:
        raise FileNotFoundError(f"dataset has no year partitions: {dataset_dir}")
    frame = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    frame["timestamp"] = _normalize_timestamp_series(frame["timestamp"])
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    frame["trade_date"] = normalize_trade_date(frame["trade_date"])
    return frame


def _window(frame: pd.DataFrame, window: Dict[str, str]) -> pd.DataFrame:
    return filter_trade_dates(frame, str(window["start"]), str(window["end"]))


def _expiry_day_mask(frame: pd.DataFrame, *, context: str) -> tuple[pd.Series, str]:
    if "ctx_is_expiry_day" in frame.columns:
        mask = pd.to_numeric(frame["ctx_is_expiry_day"], errors="coerce").fillna(0.0) == 1.0
        return mask, "ctx_is_expiry_day"
    if "ctx_dte_days" in frame.columns:
        mask = pd.to_numeric(frame["ctx_dte_days"], errors="coerce") == 0.0
        return mask.fillna(False), "ctx_dte_days"
    raise ValueError(f"{context} cannot apply block_expiry without ctx_is_expiry_day or ctx_dte_days")


def _apply_runtime_filters(
    frame: pd.DataFrame,
    *,
    block_expiry: bool,
    context: str,
    support_context: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    out = frame.copy()
    meta: Dict[str, Any] = {
        "rows_before": int(len(out)),
        "rows_after": int(len(out)),
        "expiry_rows_dropped": 0,
        "signal_column": None,
    }
    if not block_expiry:
        return out, meta
    if support_context is not None and len(support_context) > 0:
        context_columns = [
            column
            for column in support_context.columns
            if column not in KEY_COLUMNS and column not in out.columns
        ]
        if context_columns:
            support_lookup = support_context.loc[:, KEY_COLUMNS + context_columns].drop_duplicates(subset=KEY_COLUMNS)
            out = out.merge(support_lookup, on=KEY_COLUMNS, how="left")
    expiry_mask, signal_column = _expiry_day_mask(out, context=context)
    meta["signal_column"] = signal_column
    meta["expiry_rows_dropped"] = int(expiry_mask.sum())
    out = out.loc[~expiry_mask].copy().sort_values("timestamp").reset_index(drop=True)
    meta["rows_after"] = int(len(out))
    return out, meta


def _attach_support_context(frame: pd.DataFrame, support_context: Optional[pd.DataFrame]) -> pd.DataFrame:
    if support_context is None or len(frame) == 0 or len(support_context) == 0:
        return frame
    context_columns = [
        column
        for column in support_context.columns
        if column not in KEY_COLUMNS and column not in frame.columns
    ]
    if not context_columns:
        return frame
    support_lookup = support_context.loc[:, KEY_COLUMNS + context_columns].drop_duplicates(subset=KEY_COLUMNS)
    merged = frame.merge(support_lookup, on=KEY_COLUMNS, how="left")
    if len(merged) != len(frame):
        raise ValueError("support context join mismatch")
    return merged


def _recipe_cfg(recipe: LabelRecipe) -> EffectiveLabelConfig:
    return EffectiveLabelConfig(
        horizon_minutes=int(recipe.horizon_minutes),
        return_threshold=0.0,
        use_excursion_gate=False,
        min_favorable_excursion=0.0,
        max_adverse_excursion=0.0,
        stop_loss_pct=float(recipe.stop_loss_pct),
        take_profit_pct=float(recipe.take_profit_pct),
        allow_hold_extension=False,
        extension_trigger_profit_pct=0.0,
        barrier_mode="fixed",
        neutral_policy="exclude_from_primary",
        event_sampling_mode="none",
        event_signal_col=None,
        event_end_ts_mode="first_touch_or_vertical",
    )


def _safe_float(value: Any, default: float = float("nan")) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    return out if np.isfinite(out) else float(default)


def _path_return(row: pd.Series, *, prefix: str) -> float:
    reason = str(row.get(f"{prefix}_path_exit_reason") or "").strip().lower()
    if reason in {"tp", "tp_sl_same_bar"}:
        return _safe_float(row.get(f"{prefix}_barrier_upper_return"))
    if reason == "sl":
        return -abs(_safe_float(row.get(f"{prefix}_barrier_lower_return")))
    return _safe_float(row.get(f"{prefix}_forward_return"))


def _adverse_excursion(row: pd.Series, *, prefix: str) -> float:
    value = _safe_float(row.get(f"{prefix}_mae"), default=float("inf"))
    if not np.isfinite(value):
        return float("inf")
    return float(abs(min(float(value), 0.0)))


def _candidate_better(candidate: dict[str, Any], incumbent: Optional[dict[str, Any]]) -> bool:
    if incumbent is None:
        return True
    if float(candidate["net_return_after_cost"]) != float(incumbent["net_return_after_cost"]):
        return float(candidate["net_return_after_cost"]) > float(incumbent["net_return_after_cost"])
    if float(candidate["adverse_excursion"]) != float(incumbent["adverse_excursion"]):
        return float(candidate["adverse_excursion"]) < float(incumbent["adverse_excursion"])
    if int(candidate["horizon_minutes"]) != int(incumbent["horizon_minutes"]):
        return int(candidate["horizon_minutes"]) < int(incumbent["horizon_minutes"])
    if float(candidate["stop_loss_pct"]) != float(incumbent["stop_loss_pct"]):
        return float(candidate["stop_loss_pct"]) < float(incumbent["stop_loss_pct"])
    return str(candidate["recipe_id"]) < str(incumbent["recipe_id"])


def _label_recipe_frame(support: pd.DataFrame, recipe: LabelRecipe) -> pd.DataFrame:
    labeled = build_labeled_dataset(support.copy(), cfg=_recipe_cfg(recipe))
    labeled = prepare_snapshot_labeled_frame(labeled, context=f"staged:{recipe.recipe_id}")
    labeled["trade_date"] = normalize_trade_date(labeled["trade_date"])
    labeled["timestamp"] = _normalize_timestamp_series(labeled["timestamp"])
    return labeled


def _align_recipe_frame(support: pd.DataFrame, labeled: pd.DataFrame, *, recipe_id: str) -> pd.DataFrame:
    required_columns = KEY_COLUMNS + [
        "ce_label_valid",
        "pe_label_valid",
        "ce_path_exit_reason",
        "pe_path_exit_reason",
        "ce_barrier_upper_return",
        "pe_barrier_upper_return",
        "ce_barrier_lower_return",
        "pe_barrier_lower_return",
        "ce_forward_return",
        "pe_forward_return",
        "ce_mae",
        "pe_mae",
    ]
    missing_columns = [name for name in required_columns if name not in labeled.columns]
    if missing_columns:
        raise ValueError(f"recipe frame missing required columns for {recipe_id}: {missing_columns}")
    work = labeled.loc[:, required_columns].copy()
    if bool(work.duplicated(subset=KEY_COLUMNS).any()):
        raise ValueError(f"recipe frame has duplicate keys for {recipe_id}")
    aligned = support.loc[:, KEY_COLUMNS].merge(work, on=KEY_COLUMNS, how="left", sort=False, indicator=True)
    missing_rows = aligned["_merge"] != "both"
    if bool(missing_rows.any()):
        raise ValueError(
            f"recipe frame alignment mismatch for {recipe_id}: missing_rows={int(missing_rows.sum())}"
        )
    return aligned.drop(columns="_merge").reset_index(drop=True)


def _numeric_array(values: pd.Series, *, fillna: Optional[float] = 0.0) -> np.ndarray:
    series = pd.to_numeric(values, errors="coerce")
    if fillna is not None:
        series = series.fillna(float(fillna))
    return series.to_numpy(dtype=float, copy=False)


def _path_return_series(frame: pd.DataFrame, *, prefix: str) -> np.ndarray:
    reasons = frame[f"{prefix}_path_exit_reason"].astype(str).str.strip().str.lower()
    upper = _numeric_array(frame[f"{prefix}_barrier_upper_return"], fillna=np.nan)
    lower = _numeric_array(frame[f"{prefix}_barrier_lower_return"], fillna=np.nan)
    forward = _numeric_array(frame[f"{prefix}_forward_return"], fillna=np.nan)
    result = forward.copy()
    tp_mask = reasons.isin({"tp", "tp_sl_same_bar"}).to_numpy(dtype=bool, copy=False)
    sl_mask = (reasons == "sl").to_numpy(dtype=bool, copy=False)
    result[tp_mask] = upper[tp_mask]
    result[sl_mask] = -np.abs(lower[sl_mask])
    return result


def _adverse_excursion_series(frame: pd.DataFrame, *, prefix: str) -> np.ndarray:
    values = _numeric_array(frame[f"{prefix}_mae"], fillna=np.nan)
    adverse = np.full(len(values), np.inf, dtype=float)
    finite = np.isfinite(values)
    adverse[finite] = np.abs(np.minimum(values[finite], 0.0))
    return adverse


def _candidate_better_mask(
    candidate_valid: np.ndarray,
    candidate_net: np.ndarray,
    candidate_adverse: np.ndarray,
    *,
    recipe_id: str,
    horizon_minutes: int,
    stop_loss_pct: float,
    best_valid: np.ndarray,
    best_net: np.ndarray,
    best_adverse: np.ndarray,
    best_horizon: np.ndarray,
    best_stop_loss: np.ndarray,
    best_recipe: np.ndarray,
) -> np.ndarray:
    net_equal = candidate_net == best_net
    adverse_equal = candidate_adverse == best_adverse
    horizon_equal = int(horizon_minutes) == best_horizon
    stop_equal = float(stop_loss_pct) == best_stop_loss
    return candidate_valid & (
        (~best_valid)
        | (candidate_net > best_net)
        | (net_equal & (candidate_adverse < best_adverse))
        | (net_equal & adverse_equal & (int(horizon_minutes) < best_horizon))
        | (net_equal & adverse_equal & horizon_equal & (float(stop_loss_pct) < best_stop_loss))
        | (net_equal & adverse_equal & horizon_equal & stop_equal & (str(recipe_id) < best_recipe))
    )


def _build_oracle_targets(
    support: pd.DataFrame,
    recipes: Sequence[LabelRecipe],
    *,
    cost_per_trade: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if bool(support.duplicated(subset=KEY_COLUMNS).any()):
        raise ValueError("support frame contains duplicate staged oracle keys")
    utility = support.loc[:, KEY_COLUMNS].copy()
    row_count = len(support)
    best_valid = np.zeros(row_count, dtype=bool)
    best_recipe = np.full(row_count, "\uffff", dtype=object)
    best_side = np.full(row_count, "", dtype=object)
    best_direction_up = np.full(row_count, np.nan, dtype=float)
    best_net = np.full(row_count, float("-inf"), dtype=float)
    best_adverse = np.full(row_count, np.inf, dtype=float)
    best_horizon = np.full(row_count, np.iinfo(np.int32).max, dtype=np.int32)
    best_stop_loss = np.full(row_count, np.inf, dtype=float)

    for recipe in recipes:
        labeled = _align_recipe_frame(support, _label_recipe_frame(support, recipe), recipe_id=recipe.recipe_id)
        ce_net = _path_return_series(labeled, prefix="ce") - float(cost_per_trade)
        pe_net = _path_return_series(labeled, prefix="pe") - float(cost_per_trade)
        ce_valid = _numeric_array(labeled["ce_label_valid"], fillna=0.0) == 1.0
        pe_valid = _numeric_array(labeled["pe_label_valid"], fillna=0.0) == 1.0
        utility[f"{recipe.recipe_id}__ce_net_return"] = np.where(ce_valid, ce_net, np.nan)
        utility[f"{recipe.recipe_id}__pe_net_return"] = np.where(pe_valid, pe_net, np.nan)

        candidate_specs = (
            ("CE", "ce", 1, ce_net),
            ("PE", "pe", 0, pe_net),
        )
        for side_name, prefix, direction_up, net_returns in candidate_specs:
            valid = _numeric_array(labeled[f"{prefix}_label_valid"], fillna=0.0) == 1.0
            candidate_valid = valid & np.isfinite(net_returns) & (net_returns > 0.0)
            candidate_adverse = _adverse_excursion_series(labeled, prefix=prefix)
            better_mask = _candidate_better_mask(
                candidate_valid,
                net_returns,
                candidate_adverse,
                recipe_id=recipe.recipe_id,
                horizon_minutes=int(recipe.horizon_minutes),
                stop_loss_pct=float(recipe.stop_loss_pct),
                best_valid=best_valid,
                best_net=best_net,
                best_adverse=best_adverse,
                best_horizon=best_horizon,
                best_stop_loss=best_stop_loss,
                best_recipe=best_recipe,
            )
            if not bool(better_mask.any()):
                continue
            best_valid[better_mask] = True
            best_recipe[better_mask] = str(recipe.recipe_id)
            best_side[better_mask] = side_name
            best_direction_up[better_mask] = float(direction_up)
            best_net[better_mask] = net_returns[better_mask]
            best_adverse[better_mask] = candidate_adverse[better_mask]
            best_horizon[better_mask] = int(recipe.horizon_minutes)
            best_stop_loss[better_mask] = float(recipe.stop_loss_pct)

    best_ce_cols = [f"{recipe.recipe_id}__ce_net_return" for recipe in recipes]
    best_pe_cols = [f"{recipe.recipe_id}__pe_net_return" for recipe in recipes]
    utility["best_ce_net_return_after_cost"] = utility[best_ce_cols].max(axis=1)
    utility["best_pe_net_return_after_cost"] = utility[best_pe_cols].max(axis=1)
    utility["best_available_net_return_after_cost"] = utility[
        ["best_ce_net_return_after_cost", "best_pe_net_return_after_cost"]
    ].max(axis=1)

    fallback_best_net = _numeric_array(utility["best_available_net_return_after_cost"], fillna=np.nan)
    best_ce_after_cost = _numeric_array(utility["best_ce_net_return_after_cost"], fillna=0.0)
    best_pe_after_cost = _numeric_array(utility["best_pe_net_return_after_cost"], fillna=0.0)
    oracle = support.loc[:, KEY_COLUMNS].copy()
    oracle["entry_label"] = best_valid.astype(int)
    oracle["direction_label"] = np.where(best_valid, best_side, None)
    if bool(best_valid.all()):
        oracle["direction_up"] = best_direction_up.astype(np.int64, copy=False)
    else:
        direction_up_values = pd.Series(pd.array([pd.NA] * row_count, dtype="Int64"))
        if bool(best_valid.any()):
            direction_up_values.loc[best_valid] = best_direction_up[best_valid].astype(np.int64, copy=False)
        oracle["direction_up"] = direction_up_values
    oracle["recipe_label"] = np.where(best_valid, best_recipe, None)
    oracle["best_net_return_after_cost"] = np.where(best_valid, best_net, fallback_best_net)
    oracle["best_ce_net_return_after_cost"] = utility["best_ce_net_return_after_cost"].to_numpy(copy=False)
    oracle["best_pe_net_return_after_cost"] = utility["best_pe_net_return_after_cost"].to_numpy(copy=False)
    oracle["direction_return_edge_after_cost"] = np.abs(best_ce_after_cost - best_pe_after_cost)
    return oracle, utility


def _attach_labels(stage_frame: pd.DataFrame, oracle: pd.DataFrame) -> pd.DataFrame:
    joined = stage_frame.merge(oracle, on=KEY_COLUMNS, how="inner")
    if len(joined) != len(stage_frame):
        raise ValueError("stage frame/label join mismatch")
    return joined.sort_values("timestamp").reset_index(drop=True)


def build_stage1_labels(stage_frame: pd.DataFrame, oracle: pd.DataFrame, *_: Any, **__: Any) -> pd.DataFrame:
    labeled = _attach_labels(stage_frame, oracle)
    labeled["move_label_valid"] = 1.0
    labeled["move_label"] = pd.to_numeric(labeled["entry_label"], errors="coerce").fillna(0.0)
    return labeled


def build_stage2_labels(stage_frame: pd.DataFrame, oracle: pd.DataFrame, *_: Any, **__: Any) -> pd.DataFrame:
    labeled = _attach_labels(stage_frame, oracle)
    labeled = labeled[pd.to_numeric(labeled["entry_label"], errors="coerce").fillna(0.0) == 1.0].copy()
    direction = labeled["direction_label"].astype(str).str.upper()
    labeled = labeled[direction.isin({"CE", "PE"})].copy()
    direction = labeled["direction_label"].astype(str).str.upper()
    labeled["move_label_valid"] = 1.0
    labeled["move_label"] = 1.0
    labeled["move_first_hit_side"] = np.where(
        direction == "CE",
        "up",
        "down",
    )
    return labeled.sort_values("timestamp").reset_index(drop=True)


def _normalize_stage2_label_filter(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = dict((manifest.get("training") or {}).get("stage2_label_filter") or {})
    enabled = bool(raw.get("enabled", False))
    try:
        min_edge = float(raw.get("min_directional_edge_after_cost", 0.0))
    except Exception:
        min_edge = 0.0
    try:
        max_opposing = float(raw.get("max_opposing_return_after_cost", 0.0))
    except Exception:
        max_opposing = 0.0
    return {
        "enabled": enabled,
        "min_directional_edge_after_cost": max(0.0, float(min_edge)),
        "require_positive_winner_after_cost": bool(raw.get("require_positive_winner_after_cost", False)),
        "max_opposing_return_after_cost": float(max_opposing),
    }


def _normalize_stage2_session_filter(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = dict((manifest.get("training") or {}).get("stage2_session_filter") or {})
    include_buckets = []
    for item in list(raw.get("include_buckets") or []):
        bucket = str(item).strip().upper()
        if bucket and bucket not in include_buckets:
            include_buckets.append(bucket)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "include_buckets": include_buckets,
    }


def _apply_stage2_label_filter(stage2_frame: pd.DataFrame, manifest: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    cfg = _normalize_stage2_label_filter(manifest)
    if "direction_return_edge_after_cost" in stage2_frame.columns:
        edge_series = pd.to_numeric(stage2_frame["direction_return_edge_after_cost"], errors="coerce")
    else:
        edge_series = pd.Series(np.nan, index=stage2_frame.index, dtype=float)
    meta = {
        **cfg,
        "rows_before": int(len(stage2_frame)),
        "rows_after": int(len(stage2_frame)),
        "rows_dropped": 0,
        "kept_share": 1.0 if len(stage2_frame) else 0.0,
        "edge_filter_rows_dropped": 0,
        "valid_winner_rows_dropped": 0,
        "edge_mean_before": (
            float(edge_series.dropna().mean()) if bool(edge_series.notna().any()) else None
        ),
        "edge_mean_after": (
            float(edge_series.dropna().mean()) if bool(edge_series.notna().any()) else None
        ),
    }
    if not cfg["enabled"]:
        return stage2_frame.sort_values("timestamp").reset_index(drop=True), meta

    required = [
        "best_ce_net_return_after_cost",
        "best_pe_net_return_after_cost",
    ]
    missing = [column for column in required if column not in stage2_frame.columns]
    if missing:
        raise ValueError(f"stage2 label filter requires columns: {missing}")

    ce_returns = pd.to_numeric(stage2_frame["best_ce_net_return_after_cost"], errors="coerce").fillna(0.0)
    pe_returns = pd.to_numeric(stage2_frame["best_pe_net_return_after_cost"], errors="coerce").fillna(0.0)
    edge_series = (ce_returns - pe_returns).abs()
    keep_mask = edge_series >= float(cfg["min_directional_edge_after_cost"])
    edge_keep_mask = keep_mask.copy()
    valid_winner_keep_mask = pd.Series(True, index=stage2_frame.index, dtype=bool)
    if cfg["require_positive_winner_after_cost"]:
        direction = stage2_frame["direction_label"].astype(str).str.upper()
        winner_returns = np.where(direction == "CE", ce_returns, pe_returns)
        opposing_returns = np.where(direction == "CE", pe_returns, ce_returns)
        valid_winner_keep_mask = pd.Series(
            (winner_returns > 0.0) & (opposing_returns <= float(cfg["max_opposing_return_after_cost"])),
            index=stage2_frame.index,
            dtype=bool,
        )
        keep_mask = keep_mask & valid_winner_keep_mask
    filtered = stage2_frame.loc[keep_mask].copy().sort_values("timestamp").reset_index(drop=True)
    filtered["direction_return_edge_after_cost"] = edge_series.loc[keep_mask].to_numpy(copy=False)
    meta.update(
        {
            "rows_after": int(len(filtered)),
            "rows_dropped": int((~keep_mask).sum()),
            "kept_share": float(keep_mask.mean()) if len(keep_mask) else 0.0,
            "edge_filter_rows_dropped": int((~edge_keep_mask).sum()),
            "valid_winner_rows_dropped": int((edge_keep_mask & (~valid_winner_keep_mask)).sum()),
            "edge_mean_after": (
                float(edge_series.loc[keep_mask].mean()) if bool(keep_mask.any()) else None
            ),
        }
    )
    return filtered, meta


def _apply_stage2_session_filter(stage2_frame: pd.DataFrame, manifest: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    cfg = _normalize_stage2_session_filter(manifest)
    session_bucket = _stage2_time_bucket(stage2_frame)
    observed_before = sorted(str(item) for item in session_bucket.dropna().astype(str).unique().tolist())
    meta = {
        **cfg,
        "rows_before": int(len(stage2_frame)),
        "rows_after": int(len(stage2_frame)),
        "rows_dropped": 0,
        "kept_share": 1.0 if len(stage2_frame) else 0.0,
        "observed_buckets_before": observed_before,
        "observed_buckets_after": observed_before,
    }
    if not cfg["enabled"] or not cfg["include_buckets"]:
        return stage2_frame.sort_values("timestamp").reset_index(drop=True), meta
    include_buckets = set(str(item).strip().upper() for item in cfg["include_buckets"])
    keep_mask = session_bucket.isin(include_buckets)
    filtered = stage2_frame.loc[keep_mask].copy().sort_values("timestamp").reset_index(drop=True)
    observed_after = sorted(
        str(item)
        for item in _stage2_time_bucket(filtered).dropna().astype(str).unique().tolist()
    )
    meta.update(
        {
            "rows_after": int(len(filtered)),
            "rows_dropped": int((~keep_mask).sum()),
            "kept_share": float(keep_mask.mean()) if len(keep_mask) else 0.0,
            "observed_buckets_after": observed_after,
        }
    )
    return filtered, meta


def _stage2_direction_binary(direction_label: pd.Series) -> pd.Series:
    direction = pd.Series(direction_label).astype(str).str.upper()
    binary = pd.Series(np.nan, index=direction.index, dtype=float)
    binary.loc[direction == "CE"] = 1.0
    binary.loc[direction == "PE"] = 0.0
    return binary


def _stage2_probability_histogram(probabilities: pd.Series, *, bins: int = 10) -> dict[str, Any]:
    probs = pd.to_numeric(probabilities, errors="coerce").dropna().clip(0.0, 1.0)
    edges = np.linspace(0.0, 1.0, int(bins) + 1)
    if len(probs) == 0:
        return {
            "bins": [
                {"lo": round(float(edges[idx]), 4), "hi": round(float(edges[idx + 1]), 4), "count": 0, "share": 0.0}
                for idx in range(len(edges) - 1)
            ]
        }
    counts, _ = np.histogram(probs.to_numpy(dtype=float), bins=edges)
    total = float(len(probs))
    return {
        "bins": [
            {
                "lo": round(float(edges[idx]), 4),
                "hi": round(float(edges[idx + 1]), 4),
                "count": int(counts[idx]),
                "share": float(counts[idx] / total),
            }
            for idx in range(len(counts))
        ]
    }


def _quantile_summary(values: pd.Series) -> dict[str, Any]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if len(numeric) == 0:
        return {"count": 0, "mean": None, "std": None, "min": None, "p25": None, "median": None, "p75": None, "max": None}
    return {
        "count": int(len(numeric)),
        "mean": float(numeric.mean()),
        "std": float(numeric.std(ddof=0)) if len(numeric) > 1 else 0.0,
        "min": float(numeric.min()),
        "p25": float(numeric.quantile(0.25)),
        "median": float(numeric.quantile(0.5)),
        "p75": float(numeric.quantile(0.75)),
        "max": float(numeric.max()),
    }


def _stage2_score_separation(labels: pd.Series, probabilities: pd.Series) -> dict[str, Any]:
    y = pd.to_numeric(labels, errors="coerce")
    p = pd.to_numeric(probabilities, errors="coerce")
    mask = y.notna() & p.notna()
    y = y.loc[mask].astype(int)
    p = p.loc[mask].astype(float)
    pos = p.loc[y == 1]
    neg = p.loc[y == 0]
    return {
        "rows": int(len(p)),
        "positive_rows": int(len(pos)),
        "negative_rows": int(len(neg)),
        "positive_mean_prob": float(pos.mean()) if len(pos) else None,
        "negative_mean_prob": float(neg.mean()) if len(neg) else None,
        "positive_median_prob": float(pos.median()) if len(pos) else None,
        "negative_median_prob": float(neg.median()) if len(neg) else None,
        "mean_gap": float(pos.mean() - neg.mean()) if len(pos) and len(neg) else None,
        "median_gap": float(pos.median() - neg.median()) if len(pos) and len(neg) else None,
    }


def _stage2_calibration_profile(labels: pd.Series, probabilities: pd.Series, *, bins: int = 10) -> dict[str, Any]:
    y = pd.to_numeric(labels, errors="coerce")
    p = pd.to_numeric(probabilities, errors="coerce")
    mask = y.notna() & p.notna()
    y = y.loc[mask].astype(int)
    p = p.loc[mask].astype(float).clip(0.0, 1.0)
    if len(y) == 0:
        return {"calibration_error": None, "deciles": []}
    if int(p.nunique(dropna=True)) <= 1:
        avg_pred = float(p.mean())
        pos_rate = float(y.mean())
        return {
            "calibration_error": float(abs(pos_rate - avg_pred)),
            "deciles": [
                {
                    "bucket": 1,
                    "rows": int(len(y)),
                    "avg_predicted_prob": avg_pred,
                    "positive_rate": pos_rate,
                    "gap_abs": float(abs(pos_rate - avg_pred)),
                }
            ],
        }
    order = np.argsort(p.to_numpy(dtype=float))
    y_sorted = y.to_numpy(dtype=float)[order]
    p_sorted = p.to_numpy(dtype=float)[order]
    effective_bins = max(1, min(int(bins), int(len(y_sorted))))
    bucket_edges = np.linspace(0, len(y_sorted), effective_bins + 1, dtype=int)
    deciles: list[dict[str, Any]] = []
    for idx in range(effective_bins):
        lo = int(bucket_edges[idx])
        hi = int(bucket_edges[idx + 1])
        if hi <= lo:
            continue
        bucket_y = y_sorted[lo:hi]
        bucket_p = p_sorted[lo:hi]
        deciles.append(
            {
                "bucket": int(idx + 1),
                "rows": int(len(bucket_y)),
                "avg_predicted_prob": float(np.mean(bucket_p)),
                "positive_rate": float(np.mean(bucket_y)),
                "gap_abs": float(abs(np.mean(bucket_y) - np.mean(bucket_p))),
            }
        )
    return {
        "calibration_error": calibration_error(y.to_numpy(dtype=float), p.to_numpy(dtype=float), bins=effective_bins),
        "deciles": deciles,
    }


def _stage2_time_bucket(frame: pd.DataFrame) -> pd.Series:
    if "timestamp" not in frame.columns:
        return pd.Series(["UNKNOWN"] * len(frame), index=frame.index, dtype=object)
    timestamps = _normalize_timestamp_series(frame["timestamp"])
    minute_of_day = (timestamps.dt.hour * 60) + timestamps.dt.minute
    bucket = pd.Series("UNKNOWN", index=frame.index, dtype=object)
    bucket.loc[minute_of_day < (10 * 60)] = "OPENING"
    bucket.loc[(minute_of_day >= (10 * 60)) & (minute_of_day < (12 * 60))] = "MORNING"
    bucket.loc[(minute_of_day >= (12 * 60)) & (minute_of_day < (13 * 60 + 30))] = "MIDDAY"
    bucket.loc[minute_of_day >= (13 * 60 + 30)] = "LATE_SESSION"
    bucket.loc[timestamps.isna()] = "UNKNOWN"
    return bucket


def _stage2_expiry_regime(frame: pd.DataFrame) -> pd.Series:
    regime = pd.Series("UNKNOWN", index=frame.index, dtype=object)
    if "ctx_is_expiry_day" in frame.columns:
        expiry_day = pd.to_numeric(frame["ctx_is_expiry_day"], errors="coerce").fillna(0.0) == 1.0
        regime.loc[expiry_day] = "EXPIRY_DAY"
    else:
        expiry_day = pd.Series(False, index=frame.index, dtype=bool)
    near_expiry = pd.Series(False, index=frame.index, dtype=bool)
    if "ctx_regime_expiry_near" in frame.columns:
        near_expiry = near_expiry | (pd.to_numeric(frame["ctx_regime_expiry_near"], errors="coerce").fillna(0.0) == 1.0)
    if "ctx_dte_days" in frame.columns:
        dte = pd.to_numeric(frame["ctx_dte_days"], errors="coerce")
        near_expiry = near_expiry | (dte.notna() & (dte <= 1.0))
        regime.loc[dte.notna()] = "REGULAR"
    regime.loc[near_expiry & (~expiry_day)] = "NEAR_EXPIRY"
    regular_mask = (~expiry_day) & (~near_expiry)
    if regular_mask.any():
        regime.loc[regular_mask] = "REGULAR"
    return regime


def _quality_by_group(frame: pd.DataFrame, *, label_col: str, prob_col: str, group_col: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if group_col not in frame.columns:
        return rows
    for group_name, group in frame.groupby(group_col, dropna=False):
        labels = pd.to_numeric(group[label_col], errors="coerce")
        probs = pd.to_numeric(group[prob_col], errors="coerce")
        quality = _binary_quality(labels, probs)
        rows.append(
            {
                "group": str(group_name),
                "rows": int(len(group)),
                "positive_rate": float(labels.dropna().mean()) if bool(labels.notna().any()) else None,
                "roc_auc": quality["roc_auc"],
                "brier": quality["brier"],
            }
        )
    return sorted(rows, key=lambda item: item["group"])


def _stage2_scored_frame(frame: pd.DataFrame, scores: pd.DataFrame) -> pd.DataFrame:
    scored = frame.merge(scores, on=KEY_COLUMNS, how="left")
    out = scored.assign(
        direction_binary=_stage2_direction_binary(scored["direction_label"]),
        direction_up_prob=pd.to_numeric(scored["direction_up_prob"], errors="coerce"),
        session_bucket=_stage2_time_bucket(scored),
        expiry_regime=_stage2_expiry_regime(scored),
    )
    ordered_columns = [
        *KEY_COLUMNS,
        "direction_label",
        "direction_binary",
        "direction_up_prob",
        "direction_return_edge_after_cost",
        "session_bucket",
        "expiry_regime",
    ]
    return out.loc[:, [column for column in ordered_columns if column in out.columns]].copy()


def _build_stage2_split_diagnostics(frame: pd.DataFrame, scores: pd.DataFrame, *, split_name: str) -> dict[str, Any]:
    scored = _stage2_scored_frame(frame, scores)
    edge_series = pd.to_numeric(scored.get("direction_return_edge_after_cost"), errors="coerce")
    quality = _binary_quality(scored["direction_binary"], scored["direction_up_prob"])
    calibration = _stage2_calibration_profile(scored["direction_binary"], scored["direction_up_prob"])
    return {
        "split": split_name,
        "rows": int(len(scored)),
        "rows_with_probabilities": int((scored["direction_up_prob"].notna() & scored["direction_binary"].notna()).sum()),
        "positive_rate": float(scored["direction_binary"].dropna().mean()) if bool(scored["direction_binary"].notna().any()) else None,
        "quality": quality,
        "probability_histogram": _stage2_probability_histogram(scored["direction_up_prob"]),
        "score_separation": _stage2_score_separation(scored["direction_binary"], scored["direction_up_prob"]),
        "calibration": calibration,
        "edge_distribution": _quantile_summary(edge_series),
        "quality_by_time_bucket": _quality_by_group(scored, label_col="direction_binary", prob_col="direction_up_prob", group_col="session_bucket"),
        "quality_by_expiry_regime": _quality_by_group(scored, label_col="direction_binary", prob_col="direction_up_prob", group_col="expiry_regime"),
    }


def _build_stage2_diagnostics_report(
    *,
    ctx: RunContext,
    manifest: dict[str, Any],
    labeled_frames: dict[str, dict[str, pd.DataFrame]],
    stage2_result: dict[str, Any],
    label_filter_meta: dict[str, Any],
) -> dict[str, Any]:
    train_scores = _score_single_target(
        labeled_frames["stage2"]["research_train"],
        stage2_result["search_package"],
        prob_col="direction_up_prob",
    )
    score_frames = {
        "research_train": _stage2_scored_frame(
            labeled_frames["stage2"]["research_train"],
            train_scores,
        ),
        "research_valid": _stage2_scored_frame(
            labeled_frames["stage2"]["research_valid"],
            stage2_result["validation_scores"],
        ),
        "final_holdout": _stage2_scored_frame(
            labeled_frames["stage2"]["final_holdout"],
            stage2_result["holdout_scores"],
        ),
    }
    score_paths: dict[str, str] = {}
    scores_root = ctx.output_root / "stages" / "stage2" / "diagnostics_scores"
    scores_root.mkdir(parents=True, exist_ok=True)
    for split_name, score_frame in score_frames.items():
        score_path = scores_root / f"{split_name}.parquet"
        score_frame.to_parquet(score_path, index=False)
        score_paths[split_name] = str(score_path.resolve())
    split_reports = {
        split_name: {
            **_build_stage2_split_diagnostics(
                score_frame.loc[:, [column for column in score_frame.columns if column != "direction_up_prob"]],
                score_frame.loc[:, KEY_COLUMNS + ["direction_up_prob"]],
                split_name=split_name,
            ),
            "score_path": score_paths[split_name],
        }
        for split_name, score_frame in score_frames.items()
    }
    return {
        "diagnostics_schema_version": 1,
        "created_at_utc": utc_now(),
        "run_id": str(ctx.output_root.name),
        "stage": "stage2",
        "feature_sets": list(manifest["catalog"]["feature_sets_by_stage"]["stage2"]),
        "scenario": {
            "feature_set_candidates": list(manifest["catalog"]["feature_sets_by_stage"]["stage2"]),
            "selected_feature_set": str((((stage2_result.get("search_payload") or {}).get("report") or {}).get("best_experiment") or {}).get("feature_set") or ""),
            "selected_model": dict((((stage2_result.get("search_payload") or {}).get("report") or {}).get("best_experiment") or {}).get("model") or {}),
            "label_filtering": dict(label_filter_meta),
            "session_filter": _normalize_stage2_session_filter(manifest),
        },
        "label_filtering": dict(label_filter_meta),
        "score_paths": score_paths,
        "splits": split_reports,
    }


def build_stage3_labels(stage_frame: pd.DataFrame, oracle: pd.DataFrame, *_: Any, **__: Any) -> pd.DataFrame:
    labeled = _attach_labels(stage_frame, oracle)
    labeled = labeled[pd.to_numeric(labeled["entry_label"], errors="coerce").fillna(0.0) == 1.0].copy()
    labeled["chosen_direction_up"] = pd.to_numeric(labeled["direction_up"], errors="coerce").fillna(0.0)
    return labeled.sort_values("timestamp").reset_index(drop=True)


def _binary_quality(y_true: pd.Series, y_prob: pd.Series) -> dict[str, Any]:
    labels = pd.to_numeric(pd.Series(y_true), errors="coerce")
    probs = pd.to_numeric(pd.Series(y_prob), errors="coerce")
    mask = labels.notna() & probs.notna()
    labels = labels.loc[mask].astype(int)
    probs = probs.loc[mask].astype(float)
    roc = float(roc_auc_score(labels, probs)) if len(labels) >= 2 and len(labels.unique()) >= 2 else None
    brier = float(brier_score_loss(labels, probs)) if len(labels) else None
    split = len(labels) // 2
    roc_first = float(roc_auc_score(labels.iloc[:split], probs.iloc[:split])) if split >= 10 and len(labels.iloc[:split].unique()) >= 2 else None
    roc_second = float(roc_auc_score(labels.iloc[split:], probs.iloc[split:])) if (len(labels) - split) >= 10 and len(labels.iloc[split:].unique()) >= 2 else None
    return {
        "rows": int(len(labels)),
        "roc_auc": roc,
        "brier": brier,
        "roc_auc_first_half": roc_first,
        "roc_auc_second_half": roc_second,
        "roc_auc_drift_half_split": (
            float(abs(roc_first - roc_second)) if roc_first is not None and roc_second is not None else None
        ),
    }


def _stage_binary_frame(frame: pd.DataFrame, *, mode: str, label_col: str, positive_value: Any) -> pd.DataFrame:
    out = frame.copy()
    if mode == "entry":
        out["move_label_valid"] = 1.0
        out["move_label"] = pd.to_numeric(out[label_col], errors="coerce").fillna(0.0)
        return out
    if mode == "direction":
        positive = str(positive_value).upper()
        direction = out[label_col].astype(str).str.upper()
        valid_direction = direction.isin({positive, "PE"} if positive == "CE" else {positive})
        out = out.loc[valid_direction].copy()
        direction = out[label_col].astype(str).str.upper()
        out["move_label_valid"] = 1.0
        out["move_label"] = 1.0
        out["move_first_hit_side"] = np.where(
            direction == positive,
            "up",
            "down",
        )
        return out
    if mode == "recipe":
        out["move_label_valid"] = 1.0
        out["move_label"] = np.where(out[label_col].astype(str) == str(positive_value), 1.0, 0.0)
        return out
    raise ValueError(f"unsupported mode: {mode}")


def _score_single_target(frame: pd.DataFrame, package: Dict[str, Any], *, prob_col: str) -> pd.DataFrame:
    probs, _ = predict_probabilities_from_frame(frame, package, missing_policy_override="error", context=prob_col)
    out = frame.loc[:, KEY_COLUMNS].copy()
    if prob_col in probs.columns:
        source_col = prob_col
    else:
        source_col = str(probs.columns[0])
    out[prob_col] = pd.to_numeric(probs[source_col], errors="coerce")
    return out


def _training_call(
    frame: pd.DataFrame,
    *,
    objective: str,
    label_target: str,
    models: Sequence[str],
    feature_sets: Sequence[str],
    preprocess: PreprocessConfig,
    cv_config: Dict[str, Any],
    random_state: int,
    model_n_jobs: int,
    model_specs_override: Optional[Sequence[Dict[str, Any]]] = None,
    search_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return run_training_cycle_catalog(
        labeled_df=frame,
        feature_profile="all",
        objective=objective,
        train_days=int(cv_config["train_days"]),
        valid_days=int(cv_config["valid_days"]),
        test_days=int(cv_config["test_days"]),
        step_days=int(cv_config["step_days"]),
        purge_days=int(cv_config.get("purge_days", 0)),
        embargo_days=int(cv_config.get("embargo_days", 0)),
        purge_mode=str(cv_config.get("purge_mode", "days")),
        embargo_rows=int(cv_config.get("embargo_rows", 0)),
        event_end_col=cv_config.get("event_end_col"),
        random_state=int(random_state),
        preprocess_cfg=preprocess,
        label_target=label_target,
        model_whitelist=list(models),
        feature_set_whitelist=list(feature_sets),
        fit_all_final_models=False,
        model_n_jobs=int(model_n_jobs),
        model_specs_override=model_specs_override,
        search_options=search_options,
    )


def _selected_model_spec_payload(search_payload: Dict[str, Any]) -> Dict[str, Any]:
    best = dict(search_payload["report"]["best_experiment"])
    model_meta = best.get("model")
    if not isinstance(model_meta, dict):
        raise ValueError("best_experiment missing model metadata")
    return {
        "name": str(model_meta.get("name") or "").strip(),
        "family": str(model_meta.get("family") or "").strip(),
        "params": dict(model_meta.get("params") or {}),
    }


def train_binary_catalog_stage(
    *,
    stage_name: str,
    train_frame: pd.DataFrame,
    valid_frame: pd.DataFrame,
    full_model_frame: pd.DataFrame,
    holdout_frame: pd.DataFrame,
    manifest: Dict[str, Any],
    models: Sequence[str],
    feature_sets: Sequence[str],
    label_mode: str,
    positive_value: Any,
    output_root: Path,
    prob_col: str,
    policy_valid_frame: Optional[pd.DataFrame] = None,
    policy_holdout_frame: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    training_cfg = dict(manifest["training"])
    preprocess = PreprocessConfig(**dict(training_cfg["preprocess"]))
    label_col = "entry_label" if label_mode == "entry" else "direction_label"
    objective = str(training_cfg["objectives_by_stage"][stage_name])
    label_target = "move_barrier_hit" if label_mode == "entry" else "move_direction_up"
    stage_search_options = dict((training_cfg.get("search_options_by_stage") or {}).get(stage_name) or {})
    stage_root = output_root / stage_name
    stage_root.mkdir(parents=True, exist_ok=True)

    search_payload = _training_call(
        _stage_binary_frame(train_frame, mode=label_mode, label_col=label_col, positive_value=positive_value),
        objective=objective,
        label_target=label_target,
        models=models,
        feature_sets=feature_sets,
        preprocess=preprocess,
        cv_config=dict(training_cfg["cv_config"]),
        random_state=int(training_cfg["random_state"]),
        model_n_jobs=int(training_cfg["runtime"]["model_n_jobs"]),
        search_options=stage_search_options,
    )
    best_feature_set = str(search_payload["report"]["best_experiment"]["feature_set"])
    best_model_spec = _selected_model_spec_payload(search_payload)
    final_payload = _training_call(
        _stage_binary_frame(full_model_frame, mode=label_mode, label_col=label_col, positive_value=positive_value),
        objective=objective,
        label_target=label_target,
        models=[],
        feature_sets=[best_feature_set],
        preprocess=preprocess,
        cv_config=dict(training_cfg["cv_config"]),
        random_state=int(training_cfg["random_state"]),
        model_n_jobs=int(training_cfg["runtime"]["model_n_jobs"]),
        model_specs_override=[best_model_spec],
    )

    search_package = dict(search_payload["model_package"])
    final_package = dict(final_payload["model_package"])
    valid_scores = _score_single_target(valid_frame, search_package, prob_col=prob_col)
    holdout_scores = _score_single_target(holdout_frame, final_package, prob_col=prob_col)
    policy_validation_scores = (
        valid_scores
        if policy_valid_frame is None
        else _score_single_target(policy_valid_frame, search_package, prob_col=prob_col)
    )
    policy_holdout_scores = (
        holdout_scores
        if policy_holdout_frame is None
        else _score_single_target(policy_holdout_frame, final_package, prob_col=prob_col)
    )

    joblib.dump(search_package, stage_root / "selection_model.joblib")
    joblib.dump(final_package, stage_root / "model.joblib")
    (stage_root / "search_report.json").write_text(json.dumps(search_payload["report"], indent=2), encoding="utf-8")
    (stage_root / "training_report.json").write_text(json.dumps(final_payload["report"], indent=2), encoding="utf-8")
    (stage_root / "feature_contract.json").write_text(
        json.dumps(dict(final_package.get("_model_input_contract") or {}), indent=2),
        encoding="utf-8",
    )
    return {
        "stage_name": stage_name,
        "search_payload": search_payload,
        "final_payload": final_payload,
        "search_package": search_package,
        "model_package": final_package,
        "model_package_path": str((stage_root / "model.joblib").resolve()),
        "training_report_path": str((stage_root / "training_report.json").resolve()),
        "feature_contract_path": str((stage_root / "feature_contract.json").resolve()),
        "validation_scores": valid_scores,
        "holdout_scores": holdout_scores,
        "validation_policy_scores": policy_validation_scores,
        "holdout_policy_scores": policy_holdout_scores,
    }


def _merge_score_frames(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=KEY_COLUMNS)
    out = frames[0].copy()
    for frame in frames[1:]:
        out = out.merge(frame, on=KEY_COLUMNS, how="outer")
    return out.sort_values("timestamp").reset_index(drop=True)


def _score_recipe_packages(frame: pd.DataFrame, recipe_packages: Dict[str, Dict[str, Any]]) -> pd.DataFrame:
    return _merge_score_frames(
        [
            _score_single_target(frame, recipe_packages[recipe_id], prob_col=f"recipe_prob_{recipe_id}")
            for recipe_id in sorted(str(recipe_id) for recipe_id in recipe_packages.keys())
        ]
    )


def _add_upstream_probs(
    frame: pd.DataFrame,
    *,
    stage1_source_frame: pd.DataFrame,
    stage2_source_frame: pd.DataFrame,
    stage1_package: Dict[str, Any],
    stage2_package: Dict[str, Any],
) -> pd.DataFrame:
    out = frame.copy()
    stage1_scores = _score_single_target(stage1_source_frame, stage1_package, prob_col="stage1_entry_prob")
    stage2_scores = _score_single_target(stage2_source_frame, stage2_package, prob_col="stage2_direction_up_prob")
    out = out.merge(stage1_scores, on=KEY_COLUMNS, how="left")
    out = out.merge(stage2_scores, on=KEY_COLUMNS, how="left")
    missing_stage1 = out["stage1_entry_prob"].isna()
    missing_stage2 = out["stage2_direction_up_prob"].isna()
    if bool(missing_stage1.any()) or bool(missing_stage2.any()):
        missing_keys = out.loc[missing_stage1 | missing_stage2, KEY_COLUMNS].head(5).to_dict(orient="records")
        raise ValueError(
            "stage3 upstream probability alignment failed: "
            f"missing stage1={int(missing_stage1.sum())} "
            f"missing stage2={int(missing_stage2.sum())} "
            f"example_keys={missing_keys}"
        )
    out["stage2_direction_down_prob"] = 1.0 - pd.to_numeric(out["stage2_direction_up_prob"], errors="coerce")
    return out


def train_recipe_ovr_stage(
    *,
    stage_name: str,
    train_frame: pd.DataFrame,
    valid_frame: pd.DataFrame,
    full_model_frame: pd.DataFrame,
    holdout_frame: pd.DataFrame,
    manifest: Dict[str, Any],
    models: Sequence[str],
    feature_sets: Sequence[str],
    output_root: Path,
    policy_valid_frame: Optional[pd.DataFrame] = None,
    policy_holdout_frame: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    training_cfg = dict(manifest["training"])
    preprocess = PreprocessConfig(**dict(training_cfg["preprocess"]))
    objective = str(training_cfg["objectives_by_stage"][stage_name])
    recipe_ids = [recipe.recipe_id for recipe in get_recipe_catalog(str(manifest["catalog"]["recipe_catalog_id"]))]
    stage_search_options = dict((training_cfg.get("search_options_by_stage") or {}).get(stage_name) or {})
    stage_root = output_root / stage_name
    stage_root.mkdir(parents=True, exist_ok=True)
    outer_n_jobs = max(1, min(len(recipe_ids), int(joblib.cpu_count() or 1)))
    configured_model_n_jobs = int(training_cfg["runtime"]["model_n_jobs"])
    inner_model_n_jobs = 1 if outer_n_jobs > 1 else configured_model_n_jobs

    recipe_payloads: dict[str, Dict[str, Any]] = {}
    selection_recipe_packages: dict[str, Dict[str, Any]] = {}
    valid_frames: list[pd.DataFrame] = []
    holdout_frames: list[pd.DataFrame] = []
    policy_valid_frames: list[pd.DataFrame] = []
    policy_holdout_frames: list[pd.DataFrame] = []
    recipe_reports: dict[str, Any] = {}

    def _train_one_recipe(recipe_id: str) -> dict[str, Any]:
        recipe_root = stage_root / "recipes" / recipe_id
        recipe_root.mkdir(parents=True, exist_ok=True)
        search_payload = _training_call(
            _stage_binary_frame(train_frame, mode="recipe", label_col="recipe_label", positive_value=recipe_id),
            objective=objective,
            label_target="move_barrier_hit",
            models=models,
            feature_sets=feature_sets,
            preprocess=preprocess,
            cv_config=dict(training_cfg["cv_config"]),
            random_state=int(training_cfg["random_state"]),
            model_n_jobs=inner_model_n_jobs,
            search_options=stage_search_options,
        )
        best_feature_set = str(search_payload["report"]["best_experiment"]["feature_set"])
        best_model_spec = _selected_model_spec_payload(search_payload)
        final_payload = _training_call(
            _stage_binary_frame(full_model_frame, mode="recipe", label_col="recipe_label", positive_value=recipe_id),
            objective=objective,
            label_target="move_barrier_hit",
            models=[],
            feature_sets=[best_feature_set],
            preprocess=preprocess,
            cv_config=dict(training_cfg["cv_config"]),
            random_state=int(training_cfg["random_state"]),
            model_n_jobs=inner_model_n_jobs,
            model_specs_override=[best_model_spec],
        )
        search_package = dict(search_payload["model_package"])
        final_package = dict(final_payload["model_package"])
        prob_col = f"recipe_prob_{recipe_id}"
        valid_scores = _score_single_target(valid_frame, search_package, prob_col=prob_col)
        holdout_scores = _score_single_target(holdout_frame, final_package, prob_col=prob_col)
        policy_validation_scores = (
            valid_scores
            if policy_valid_frame is None
            else _score_single_target(policy_valid_frame, search_package, prob_col=prob_col)
        )
        policy_holdout_scores = (
            holdout_scores
            if policy_holdout_frame is None
            else _score_single_target(policy_holdout_frame, final_package, prob_col=prob_col)
        )
        joblib.dump(final_package, recipe_root / "model.joblib")
        (recipe_root / "training_report.json").write_text(json.dumps(final_payload["report"], indent=2), encoding="utf-8")
        (recipe_root / "feature_contract.json").write_text(
            json.dumps(dict(final_package.get("_model_input_contract") or {}), indent=2),
            encoding="utf-8",
        )
        return {
            "recipe_id": recipe_id,
            "selection_package": search_package,
            "model_package": final_package,
            "valid_scores": valid_scores,
            "holdout_scores": holdout_scores,
            "policy_validation_scores": policy_validation_scores,
            "policy_holdout_scores": policy_holdout_scores,
            "artifacts": {
                "model_package_path": str((recipe_root / "model.joblib").resolve()),
                "training_report_path": str((recipe_root / "training_report.json").resolve()),
                "feature_contract_path": str((recipe_root / "feature_contract.json").resolve()),
            },
        }

    try:
        recipe_results = joblib.Parallel(n_jobs=outer_n_jobs, prefer="threads")(
            joblib.delayed(_train_one_recipe)(recipe_id) for recipe_id in recipe_ids
        )
    except PermissionError:
        recipe_results = [_train_one_recipe(recipe_id) for recipe_id in recipe_ids]
    for recipe_result in recipe_results:
        recipe_id = str(recipe_result["recipe_id"])
        selection_recipe_packages[recipe_id] = dict(recipe_result["selection_package"])
        recipe_payloads[recipe_id] = dict(recipe_result["model_package"])
        valid_frames.append(pd.DataFrame(recipe_result["valid_scores"]))
        holdout_frames.append(pd.DataFrame(recipe_result["holdout_scores"]))
        policy_valid_frames.append(pd.DataFrame(recipe_result["policy_validation_scores"]))
        policy_holdout_frames.append(pd.DataFrame(recipe_result["policy_holdout_scores"]))
        recipe_reports[recipe_id] = dict(recipe_result["artifacts"])
    summary = {
        "recipe_ids": recipe_ids,
        "reports": recipe_reports,
    }
    (stage_root / "training_report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return {
        "stage_name": stage_name,
        "selection_recipe_packages": selection_recipe_packages,
        "recipe_packages": recipe_payloads,
        "recipe_artifacts": recipe_reports,
        "training_report_path": str((stage_root / "training_report.json").resolve()),
        "validation_scores": _merge_score_frames(valid_frames),
        "holdout_scores": _merge_score_frames(holdout_frames),
        "validation_policy_scores": _merge_score_frames(policy_valid_frames),
        "holdout_policy_scores": _merge_score_frames(policy_holdout_frames),
    }


def _summarize_returns(
    returns: Sequence[float],
    *,
    rows_total: int,
    sides: Optional[Sequence[str]] = None,
    selected_recipes: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    clean = [float(value) for value in returns if np.isfinite(float(value))]
    trades = int(len(clean))
    side_list = [str(side) for side in list(sides or [])][:trades]
    recipe_list = [str(recipe) for recipe in list(selected_recipes or [])][:trades]
    ce_trades = int(sum(1 for side in side_list if side == "CE"))
    long_share = float(ce_trades / trades) if trades else 0.0
    return {
        "rows_total": int(rows_total),
        "trades": trades,
        "block_rate": float((rows_total - trades) / rows_total) if rows_total > 0 else 0.0,
        "net_return_sum": float(sum(clean)),
        "profit_factor": float(profit_factor(clean)),
        "max_drawdown_pct": float(max_drawdown_pct(clean)),
        "win_rate": float(np.mean(np.asarray(clean) > 0.0)) if clean else 0.0,
        "long_share": long_share,
        "short_share": float(1.0 - long_share) if trades else 0.0,
        "side_share_in_band": bool(0.30 <= long_share <= 0.70) if trades else False,
        "selected_recipes": sorted(set(recipe_list)),
    }


def _regime_label_series(frame: pd.DataFrame) -> pd.Series:
    if len(frame) == 0:
        return pd.Series(dtype=object)

    out = pd.Series("UNKNOWN", index=frame.index, dtype=object)
    if "regime" in frame.columns:
        explicit_source = frame["regime"]
        explicit = explicit_source.astype(str).str.strip().str.upper()
        explicit_mask = explicit_source.notna() & ~explicit.isin({"", "NAN", "NONE", "NULL"})
        out.loc[explicit_mask] = explicit.loc[explicit_mask]

    def _flag(columns: Sequence[str]) -> pd.Series:
        for column in columns:
            if column in frame.columns:
                return pd.to_numeric(frame[column], errors="coerce").fillna(0.0) == 1.0
        return pd.Series(False, index=frame.index, dtype=bool)

    expiry_near = _flag(["ctx_is_expiry_day", "ctx_regime_expiry_near", "regime_expiry_near"])
    trending = _flag(["ctx_regime_trend_up", "ctx_regime_trend_down", "regime_trend_up", "regime_trend_down"])
    sideways = _flag(["ctx_regime_atr_low", "regime_atr_low"])
    volatile = _flag(["ctx_regime_atr_high", "regime_atr_high"])

    fallback_mask = out.eq("UNKNOWN")
    out.loc[fallback_mask & expiry_near] = "PRE_EXPIRY"
    out.loc[fallback_mask & ~expiry_near & trending] = "TRENDING"
    out.loc[fallback_mask & ~expiry_near & ~trending & sideways] = "SIDEWAYS"
    out.loc[fallback_mask & ~expiry_near & ~trending & ~sideways & volatile] = "VOLATILE"
    return out


def _distribution_from_series(values: pd.Series) -> Optional[dict[str, float]]:
    if len(values) == 0:
        return None
    clean = values.astype(str).str.strip()
    clean = clean[clean != ""]
    if len(clean) == 0:
        return None
    counts = clean.value_counts(dropna=False)
    total = float(counts.sum())
    if total <= 0:
        return None
    return {str(key): float(value / total) for key, value in counts.items()}


def _expiry_segment_series(frame: pd.DataFrame) -> pd.Series:
    if len(frame) == 0:
        return pd.Series(dtype=object)

    out = pd.Series("REGULAR", index=frame.index, dtype=object)
    expiry_day = pd.Series(False, index=frame.index, dtype=bool)
    if "ctx_is_expiry_day" in frame.columns:
        expiry_day = pd.to_numeric(frame["ctx_is_expiry_day"], errors="coerce").fillna(0.0) == 1.0
    elif "ctx_dte_days" in frame.columns:
        expiry_day = pd.to_numeric(frame["ctx_dte_days"], errors="coerce").fillna(np.nan) == 0.0

    near_expiry = pd.Series(False, index=frame.index, dtype=bool)
    for column in ("ctx_is_near_expiry", "ctx_regime_expiry_near", "regime_expiry_near"):
        if column in frame.columns:
            near_expiry = pd.to_numeric(frame[column], errors="coerce").fillna(0.0) == 1.0
            break
    else:
        if "ctx_dte_days" in frame.columns:
            dte_days = pd.to_numeric(frame["ctx_dte_days"], errors="coerce")
            near_expiry = ((dte_days >= 0.0) & (dte_days <= 1.0)).fillna(False)

    out.loc[near_expiry] = "NEAR_EXPIRY"
    out.loc[expiry_day] = "EXPIRY_DAY"
    return out


def _session_segment_series(frame: pd.DataFrame) -> pd.Series:
    if len(frame) == 0:
        return pd.Series(dtype=object)

    minutes_since_open = pd.Series(np.nan, index=frame.index, dtype=float)
    if "minutes_since_open" in frame.columns:
        minutes_since_open = pd.to_numeric(frame["minutes_since_open"], errors="coerce")
    elif "minute_of_day" in frame.columns:
        minutes_since_open = pd.to_numeric(frame["minute_of_day"], errors="coerce") - float((9 * 60) + 15)
    elif "time_minute_of_day" in frame.columns:
        minutes_since_open = pd.to_numeric(frame["time_minute_of_day"], errors="coerce") - float((9 * 60) + 15)
    elif "timestamp" in frame.columns:
        timestamp = _normalize_timestamp_series(frame["timestamp"])
        minutes_since_open = (
            pd.to_numeric(timestamp.dt.hour, errors="coerce") * 60.0
            + pd.to_numeric(timestamp.dt.minute, errors="coerce")
            - float((9 * 60) + 15)
        )

    out = pd.Series("MID_SESSION", index=frame.index, dtype=object)
    out.loc[minutes_since_open < 60.0] = "FIRST_HOUR"
    out.loc[minutes_since_open >= 315.0] = "LAST_HOUR"
    return out


def _scenario_bucket_summary(
    base_frame: pd.DataFrame,
    trade_rows: pd.DataFrame,
    segment_labels: pd.Series,
    *,
    segment_order: Sequence[str],
) -> dict[str, Any]:
    base = base_frame.loc[:, KEY_COLUMNS].copy()
    base["__segment"] = segment_labels.astype(str).fillna("UNKNOWN")

    trade = pd.DataFrame(columns=KEY_COLUMNS + ["selected_return", "selected_side", "selected_recipe", "__segment"])
    if len(trade_rows) > 0:
        trade = trade_rows.loc[:, KEY_COLUMNS + ["selected_return", "selected_side", "selected_recipe"]].merge(
            base,
            on=KEY_COLUMNS,
            how="left",
        )
        trade["__segment"] = trade["__segment"].fillna("UNKNOWN")

    rows_total = int(len(base))
    segments: dict[str, Any] = {}
    for segment_name in segment_order:
        base_mask = base["__segment"] == str(segment_name)
        trade_mask = trade["__segment"] == str(segment_name)
        summary = _summarize_returns(
            trade.loc[trade_mask, "selected_return"].tolist(),
            rows_total=int(base_mask.sum()),
            sides=trade.loc[trade_mask, "selected_side"].tolist(),
            selected_recipes=trade.loc[trade_mask, "selected_recipe"].tolist(),
        )
        summary["rows_share"] = float(summary["rows_total"] / rows_total) if rows_total > 0 else 0.0
        segments[str(segment_name)] = summary
    return {
        "segment_order": list(segment_order),
        "segments": segments,
    }


def _scenario_reports(
    *,
    base_frame: pd.DataFrame,
    trade_rows: Optional[pd.DataFrame],
    evaluation_mode: str,
) -> dict[str, Any]:
    trade_frame = trade_rows if trade_rows is not None else pd.DataFrame(columns=KEY_COLUMNS + ["selected_return", "selected_side", "selected_recipe"])
    return {
        "evaluation_mode": str(evaluation_mode),
        "base_rows_total": int(len(base_frame)),
        "trade_rows_total": int(len(trade_frame)),
        "regime": _scenario_bucket_summary(
            base_frame,
            trade_frame,
            _regime_label_series(base_frame),
            segment_order=["TRENDING", "SIDEWAYS", "VOLATILE", "PRE_EXPIRY", "UNKNOWN"],
        ),
        "expiry": _scenario_bucket_summary(
            base_frame,
            trade_frame,
            _expiry_segment_series(base_frame),
            segment_order=["EXPIRY_DAY", "NEAR_EXPIRY", "REGULAR"],
        ),
        "session": _scenario_bucket_summary(
            base_frame,
            trade_frame,
            _session_segment_series(base_frame),
            segment_order=["FIRST_HOUR", "MID_SESSION", "LAST_HOUR"],
        ),
    }


def _merge_policy_inputs(base_frame: pd.DataFrame, *frames: pd.DataFrame) -> pd.DataFrame:
    out = base_frame.copy()
    for frame in frames:
        out = out.merge(frame, on=KEY_COLUMNS, how="left")
    return out.sort_values("timestamp").reset_index(drop=True)


def _direction_trade_masks(
    entry_prob: np.ndarray,
    direction_up_prob: np.ndarray,
    *,
    entry_threshold: float,
    ce_threshold: float,
    pe_threshold: float,
    min_edge: float,
) -> tuple[np.ndarray, np.ndarray]:
    ce_prob = direction_up_prob.astype(float, copy=False)
    pe_prob = 1.0 - ce_prob
    entry_mask = entry_prob >= float(entry_threshold)
    ce_ok = ce_prob >= float(ce_threshold)
    pe_ok = pe_prob >= float(pe_threshold)
    edge_ok = np.abs(ce_prob - pe_prob) >= float(min_edge)
    ce_mask = entry_mask & ce_ok & (~pe_ok | (edge_ok & (ce_prob >= pe_prob)))
    pe_mask = entry_mask & pe_ok & (~ce_ok | (edge_ok & (pe_prob > ce_prob)))
    return ce_mask, pe_mask


def select_entry_policy(valid_scores: pd.DataFrame, utility: pd.DataFrame, policy_config: Dict[str, Any]) -> dict[str, Any]:
    merged = _merge_policy_inputs(utility, valid_scores)
    entry_probs = _numeric_array(merged["entry_prob"], fillna=0.0)
    best_returns = _numeric_array(merged["best_available_net_return_after_cost"], fillna=0.0)
    rows_total = len(merged)
    rows = []
    for threshold in list(policy_config.get("threshold_grid") or []):
        mask = entry_probs >= float(threshold)
        summary = _summarize_returns(best_returns[mask].tolist(), rows_total=rows_total)
        summary["threshold"] = float(threshold)
        rows.append(summary)
    if not rows:
        raise ValueError("stage1 policy threshold_grid must not be empty")
    best = max(rows, key=lambda row: (float(row["net_return_sum"]), float(row["profit_factor"]), int(row["trades"]), -float(row["threshold"])))
    return {
        "policy_id": "entry_threshold_v1",
        "selected_threshold": float(best["threshold"]),
        "validation_rows": rows,
        "selected_validation_summary": best,
    }


def _choose_side(direction_up_prob: float, *, ce_threshold: float, pe_threshold: float, min_edge: float) -> Optional[str]:
    ce_prob = float(direction_up_prob)
    pe_prob = float(1.0 - direction_up_prob)
    ce_ok = ce_prob >= float(ce_threshold)
    pe_ok = pe_prob >= float(pe_threshold)
    if ce_ok and pe_ok:
        if abs(ce_prob - pe_prob) < float(min_edge):
            return None
        return "CE" if ce_prob >= pe_prob else "PE"
    if ce_ok:
        return "CE"
    if pe_ok:
        return "PE"
    return None


def select_direction_policy(
    valid_scores: pd.DataFrame,
    utility: pd.DataFrame,
    stage1_scores: pd.DataFrame,
    stage1_policy: Dict[str, Any],
    policy_config: Dict[str, Any],
) -> dict[str, Any]:
    merged = _merge_policy_inputs(utility, stage1_scores, valid_scores)
    entry_threshold = float(stage1_policy["selected_threshold"])
    entry_probs = _numeric_array(merged["entry_prob"], fillna=0.0)
    direction_up_prob = _numeric_array(merged["direction_up_prob"], fillna=0.0)
    ce_returns = _numeric_array(merged["best_ce_net_return_after_cost"], fillna=0.0)
    pe_returns = _numeric_array(merged["best_pe_net_return_after_cost"], fillna=0.0)
    rows_total = len(merged)
    rows = []
    for ce_threshold in list(policy_config.get("ce_threshold_grid") or []):
        for pe_threshold in list(policy_config.get("pe_threshold_grid") or []):
            for min_edge in list(policy_config.get("min_edge_grid") or []):
                ce_mask, pe_mask = _direction_trade_masks(
                    entry_probs,
                    direction_up_prob,
                    entry_threshold=entry_threshold,
                    ce_threshold=float(ce_threshold),
                    pe_threshold=float(pe_threshold),
                    min_edge=float(min_edge),
                )
                trade_mask = ce_mask | pe_mask
                returns = np.where(ce_mask, ce_returns, pe_returns)[trade_mask].tolist()
                sides = np.where(ce_mask[trade_mask], "CE", "PE").tolist()
                summary = _summarize_returns(returns, rows_total=rows_total, sides=sides)
                summary.update(
                    {
                        "ce_threshold": float(ce_threshold),
                        "pe_threshold": float(pe_threshold),
                        "min_edge": float(min_edge),
                    }
                )
                rows.append(summary)
    if not rows:
        raise ValueError("stage2 policy grids must not be empty")
    best = max(
        rows,
        key=lambda row: (
            float(row["net_return_sum"]),
            float(row["profit_factor"]),
            int(row["trades"]),
            -float(row["min_edge"]),
        ),
    )
    return {
        "policy_id": "direction_dual_threshold_v1",
        "selected_ce_threshold": float(best["ce_threshold"]),
        "selected_pe_threshold": float(best["pe_threshold"]),
        "selected_min_edge": float(best["min_edge"]),
        "validation_rows": rows,
        "selected_validation_summary": best,
    }


def _choose_recipe(row: dict[str, Any], recipe_ids: Sequence[str], *, threshold: float, margin_min: float) -> Optional[str]:
    ranked = sorted(
        ((recipe_id, _safe_float(row.get(f"recipe_prob_{recipe_id}"), default=float("-inf"))) for recipe_id in recipe_ids),
        key=lambda item: (-float(item[1]), str(item[0])),
    )
    if not ranked:
        return None
    top_id, top_prob = ranked[0]
    second_prob = ranked[1][1] if len(ranked) > 1 else 0.0
    if not np.isfinite(top_prob) or top_prob < float(threshold):
        return None
    if float(top_prob - second_prob) < float(margin_min):
        return None
    return str(top_id)


def _evaluate_combined_policy(
    utility: pd.DataFrame,
    stage1_scores: pd.DataFrame,
    stage2_scores: pd.DataFrame,
    stage3_scores: pd.DataFrame,
    *,
    stage1_threshold: float,
    ce_threshold: float,
    pe_threshold: float,
    min_edge: float,
    recipe_threshold: float,
    recipe_margin_min: float,
    recipe_ids: Sequence[str],
) -> dict[str, Any]:
    merged = _merge_policy_inputs(utility, stage1_scores, stage2_scores, stage3_scores)
    rows_total = len(merged)
    ordered_recipe_ids = sorted(str(recipe_id) for recipe_id in recipe_ids)
    if not ordered_recipe_ids:
        raise ValueError("stage3 policy recipe_ids must not be empty")

    entry_probs = _numeric_array(merged["entry_prob"], fillna=0.0)
    direction_available = pd.to_numeric(merged["direction_up_prob"], errors="coerce").notna().to_numpy(dtype=bool, copy=False)
    direction_up_prob = _numeric_array(merged["direction_up_prob"], fillna=0.5)
    ce_mask, pe_mask = _direction_trade_masks(
        entry_probs,
        direction_up_prob,
        entry_threshold=float(stage1_threshold),
        ce_threshold=float(ce_threshold),
        pe_threshold=float(pe_threshold),
        min_edge=float(min_edge),
    )
    ce_mask = ce_mask & direction_available
    pe_mask = pe_mask & direction_available
    side_mask = ce_mask | pe_mask

    recipe_prob_matrix = np.column_stack(
        [_numeric_array(merged[f"recipe_prob_{recipe_id}"], fillna=float("-inf")) for recipe_id in ordered_recipe_ids]
    )
    top_idx = np.argmax(recipe_prob_matrix, axis=1)
    row_idx = np.arange(rows_total, dtype=int)
    top_prob = recipe_prob_matrix[row_idx, top_idx]
    if len(ordered_recipe_ids) > 1:
        second_prob = np.partition(recipe_prob_matrix, -2, axis=1)[:, -2]
    else:
        second_prob = np.zeros(rows_total, dtype=float)
    recipe_valid = (
        np.isfinite(top_prob)
        & (top_prob >= float(recipe_threshold))
        & ((top_prob - second_prob) >= float(recipe_margin_min))
    )
    chosen_recipes = np.asarray(ordered_recipe_ids, dtype=object)[top_idx]

    ce_return_matrix = np.column_stack(
        [_numeric_array(merged[f"{recipe_id}__ce_net_return"], fillna=0.0) for recipe_id in ordered_recipe_ids]
    )
    pe_return_matrix = np.column_stack(
        [_numeric_array(merged[f"{recipe_id}__pe_net_return"], fillna=0.0) for recipe_id in ordered_recipe_ids]
    )
    selected_returns = np.where(
        ce_mask,
        ce_return_matrix[row_idx, top_idx],
        pe_return_matrix[row_idx, top_idx],
    )
    trade_mask = side_mask & recipe_valid
    returns = selected_returns[trade_mask].tolist()
    sides = np.where(ce_mask[trade_mask], "CE", "PE").tolist()
    recipes = chosen_recipes[trade_mask].tolist()
    summary = _summarize_returns(returns, rows_total=rows_total, sides=sides, selected_recipes=recipes)
    summary["recipe_threshold"] = float(recipe_threshold)
    summary["recipe_margin_min"] = float(recipe_margin_min)
    return summary


def _combined_policy_trade_rows(
    base_frame: pd.DataFrame,
    utility: pd.DataFrame,
    stage1_scores: pd.DataFrame,
    stage2_scores: pd.DataFrame,
    stage3_scores: pd.DataFrame,
    *,
    stage1_threshold: float,
    ce_threshold: float,
    pe_threshold: float,
    min_edge: float,
    recipe_threshold: float,
    recipe_margin_min: float,
    recipe_ids: Sequence[str],
) -> pd.DataFrame:
    merged = _merge_policy_inputs(base_frame, utility, stage1_scores, stage2_scores, stage3_scores)
    if len(merged) == 0:
        return merged

    ordered_recipe_ids = sorted(str(recipe_id) for recipe_id in recipe_ids)
    if not ordered_recipe_ids:
        raise ValueError("stage3 policy recipe_ids must not be empty")

    entry_probs = _numeric_array(merged["entry_prob"], fillna=0.0)
    direction_available = pd.to_numeric(merged["direction_up_prob"], errors="coerce").notna().to_numpy(dtype=bool, copy=False)
    direction_up_prob = _numeric_array(merged["direction_up_prob"], fillna=0.5)
    ce_mask, pe_mask = _direction_trade_masks(
        entry_probs,
        direction_up_prob,
        entry_threshold=float(stage1_threshold),
        ce_threshold=float(ce_threshold),
        pe_threshold=float(pe_threshold),
        min_edge=float(min_edge),
    )
    ce_mask = ce_mask & direction_available
    pe_mask = pe_mask & direction_available
    side_mask = ce_mask | pe_mask

    recipe_prob_matrix = np.column_stack(
        [_numeric_array(merged[f"recipe_prob_{recipe_id}"], fillna=float("-inf")) for recipe_id in ordered_recipe_ids]
    )
    top_idx = np.argmax(recipe_prob_matrix, axis=1)
    row_idx = np.arange(len(merged), dtype=int)
    top_prob = recipe_prob_matrix[row_idx, top_idx]
    if len(ordered_recipe_ids) > 1:
        second_prob = np.partition(recipe_prob_matrix, -2, axis=1)[:, -2]
    else:
        second_prob = np.zeros(len(merged), dtype=float)
    recipe_valid = (
        np.isfinite(top_prob)
        & (top_prob >= float(recipe_threshold))
        & ((top_prob - second_prob) >= float(recipe_margin_min))
    )
    chosen_recipes = np.asarray(ordered_recipe_ids, dtype=object)[top_idx]

    ce_return_matrix = np.column_stack(
        [_numeric_array(merged[f"{recipe_id}__ce_net_return"], fillna=0.0) for recipe_id in ordered_recipe_ids]
    )
    pe_return_matrix = np.column_stack(
        [_numeric_array(merged[f"{recipe_id}__pe_net_return"], fillna=0.0) for recipe_id in ordered_recipe_ids]
    )
    selected_returns = np.where(
        ce_mask,
        ce_return_matrix[row_idx, top_idx],
        pe_return_matrix[row_idx, top_idx],
    )
    trade_mask = side_mask & recipe_valid
    selected = merged.loc[trade_mask].copy()
    if len(selected) == 0:
        selected["selected_side"] = pd.Series(dtype=object)
        selected["selected_recipe"] = pd.Series(dtype=object)
        selected["selected_return"] = pd.Series(dtype=float)
        return selected

    selected["selected_side"] = np.where(ce_mask[trade_mask], "CE", "PE")
    selected["selected_recipe"] = chosen_recipes[trade_mask]
    selected["selected_return"] = selected_returns[trade_mask]
    return selected


def select_recipe_policy(
    valid_scores: pd.DataFrame,
    utility: pd.DataFrame,
    stage1_scores: pd.DataFrame,
    stage2_scores: pd.DataFrame,
    stage1_policy: Dict[str, Any],
    stage2_policy: Dict[str, Any],
    policy_config: Dict[str, Any],
    recipe_ids: Sequence[str],
) -> dict[str, Any]:
    rows = []
    for threshold in list(policy_config.get("threshold_grid") or []):
        for margin_min in list(policy_config.get("margin_grid") or []):
            summary = _evaluate_combined_policy(
                utility,
                stage1_scores,
                stage2_scores,
                valid_scores,
                stage1_threshold=float(stage1_policy["selected_threshold"]),
                ce_threshold=float(stage2_policy["selected_ce_threshold"]),
                pe_threshold=float(stage2_policy["selected_pe_threshold"]),
                min_edge=float(stage2_policy["selected_min_edge"]),
                recipe_threshold=float(threshold),
                recipe_margin_min=float(margin_min),
                recipe_ids=recipe_ids,
            )
            rows.append(summary)
    if not rows:
        raise ValueError("stage3 policy grids must not be empty")
    best = max(rows, key=lambda row: (float(row["net_return_sum"]), float(row["profit_factor"]), int(row["trades"]), -float(row["recipe_margin_min"])))
    return {
        "policy_id": "recipe_top_margin_v1",
        "selected_threshold": float(best["recipe_threshold"]),
        "selected_margin_min": float(best["recipe_margin_min"]),
        "validation_rows": rows,
        "selected_validation_summary": best,
    }


def _fixed_recipe_baseline(
    utility: pd.DataFrame,
    stage1_scores: pd.DataFrame,
    stage2_scores: pd.DataFrame,
    *,
    stage1_threshold: float,
    ce_threshold: float,
    pe_threshold: float,
    min_edge: float,
    recipe_id: str,
) -> dict[str, Any]:
    merged = _merge_policy_inputs(utility, stage1_scores, stage2_scores)
    entry_probs = _numeric_array(merged["entry_prob"], fillna=0.0)
    direction_available = pd.to_numeric(merged["direction_up_prob"], errors="coerce").notna().to_numpy(dtype=bool, copy=False)
    direction_up_prob = _numeric_array(merged["direction_up_prob"], fillna=0.5)
    ce_returns = _numeric_array(merged[f"{recipe_id}__ce_net_return"], fillna=0.0)
    pe_returns = _numeric_array(merged[f"{recipe_id}__pe_net_return"], fillna=0.0)
    ce_mask, pe_mask = _direction_trade_masks(
        entry_probs,
        direction_up_prob,
        entry_threshold=float(stage1_threshold),
        ce_threshold=float(ce_threshold),
        pe_threshold=float(pe_threshold),
        min_edge=float(min_edge),
    )
    ce_mask = ce_mask & direction_available
    pe_mask = pe_mask & direction_available
    trade_mask = ce_mask | pe_mask
    returns = np.where(ce_mask, ce_returns, pe_returns)[trade_mask].tolist()
    sides = np.where(ce_mask[trade_mask], "CE", "PE").tolist()
    summary = _summarize_returns(
        returns,
        rows_total=len(merged),
        sides=sides,
        selected_recipes=[recipe_id] * len(returns),
    )
    summary["recipe_id"] = str(recipe_id)
    return summary


def _stage_gate_result(quality: dict[str, Any], gates: dict[str, Any], *, prefix: str = "") -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if gates.get("roc_auc_min") is not None:
        if quality.get("roc_auc") is None:
            reasons.append(f"{prefix}roc_auc_unavailable")
        elif float(quality["roc_auc"]) < float(gates["roc_auc_min"]):
            reasons.append(f"{prefix}roc_auc<{gates['roc_auc_min']}")
    if gates.get("brier_max") is not None:
        if quality.get("brier") is None:
            reasons.append(f"{prefix}brier_unavailable")
        elif float(quality["brier"]) > float(gates["brier_max"]):
            reasons.append(f"{prefix}brier>{gates['brier_max']}")
    if gates.get("roc_auc_drift_half_split_max_abs") is not None:
        if quality.get("roc_auc_drift_half_split") is None:
            reasons.append(f"{prefix}roc_auc_drift_unavailable")
        elif float(quality["roc_auc_drift_half_split"]) > float(gates["roc_auc_drift_half_split_max_abs"]):
            reasons.append(f"{prefix}roc_auc_drift>{gates['roc_auc_drift_half_split_max_abs']}")
    return (not reasons), reasons


def _check_stage2_signal(stage2_frame: pd.DataFrame) -> dict[str, Any]:
    labeled = stage2_frame[pd.to_numeric(stage2_frame["entry_label"], errors="coerce") == 1.0].copy()
    min_samples = 100
    if len(labeled) < min_samples:
        return {
            "has_signal": False,
            "reason": f"insufficient_samples: {len(labeled)}<{min_samples}",
            "samples": int(len(labeled)),
            "max_correlation": None,
            "top_features": [],
        }

    direction_binary = (labeled["direction_label"].astype(str).str.upper() == "CE").astype(int)
    exclude = set(KEY_COLUMNS) | {
        "entry_label",
        "direction_label",
        "direction_up",
        "recipe_label",
        "best_net_return_after_cost",
        "best_ce_net_return_after_cost",
        "best_pe_net_return_after_cost",
        "direction_return_edge_after_cost",
        "move_label",
        "move_label_valid",
        "move_first_hit_side",
        "chosen_direction_up",
    }
    feature_cols = [column for column in labeled.columns if column not in exclude]

    correlations: dict[str, float] = {}
    for column in feature_cols:
        values = pd.to_numeric(labeled[column], errors="coerce")
        aligned = pd.concat(
            [
                values.rename("feature"),
                direction_binary.rename("direction"),
            ],
            axis=1,
        ).dropna()
        if len(aligned) < 50:
            continue
        if float(aligned["feature"].std(ddof=0)) == 0.0 or float(aligned["direction"].std(ddof=0)) == 0.0:
            continue
        corr = abs(float(aligned["feature"].corr(aligned["direction"])))
        if np.isfinite(corr):
            correlations[column] = corr

    threshold = 0.05
    max_corr = max(correlations.values()) if correlations else 0.0
    top_features = sorted(correlations, key=lambda name: (-correlations[name], name))[:10]
    return {
        "has_signal": max_corr >= threshold,
        "reason": None if max_corr >= threshold else f"max_corr={max_corr:.4f}<{threshold}",
        "samples": int(len(labeled)),
        "max_correlation": float(max_corr),
        "top_features": [
            {"feature": feature_name, "abs_corr": round(float(correlations[feature_name]), 4)}
            for feature_name in top_features
        ],
    }


def _early_hold_summary(
    ctx: RunContext,
    manifest: dict[str, Any],
    components: dict[str, Any],
    *,
    blocking_reasons: list[str],
    completed_stage_artifacts: dict[str, Any],
    cv_prechecks: dict[str, Any],
    completion_mode: str,
    parquet_root: Path,
    support_dataset: str,
    runtime_block_expiry: bool,
    runtime_filtering: dict[str, Any],
    label_filtering: dict[str, Any],
    scenario_reports: dict[str, Any],
    training_environment: dict[str, Any],
) -> dict[str, Any]:
    summary = {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at_utc": utc_now(),
        "status": "completed",
        "experiment_kind": str(manifest["experiment_kind"]),
        "run_id": str(ctx.output_root.name),
        "parquet_root": str(parquet_root),
        "support_dataset": support_dataset,
        "recipe_catalog_id": str(manifest["catalog"]["recipe_catalog_id"]),
        "component_ids": components,
        "completion_mode": str(completion_mode),
        "cv_prechecks": cv_prechecks,
        "publish_assessment": {
            "decision": "HOLD",
            "publishable": False,
            "blocking_reasons": list(blocking_reasons),
        },
        "runtime_prefilter_gate_ids": list(manifest["runtime"]["prefilter_gate_ids"]),
        "runtime_block_expiry": bool(runtime_block_expiry),
        "runtime_filtering": runtime_filtering,
        "label_filtering": label_filtering,
        "scenario_reports": scenario_reports,
        "training_environment": dict(training_environment),
        "stage_artifacts": completed_stage_artifacts,
    }
    ctx.write_json("summary.json", summary)
    ctx.append_state(
        "job_hold",
        status="completed",
        completion_mode=str(completion_mode),
        blocking_reasons=list(blocking_reasons),
    )
    return summary


def _combined_gate_result(summary: dict[str, Any], gates: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if float(summary.get("profit_factor", 0.0)) < float(gates.get("profit_factor_min", 0.0)):
        reasons.append(f"profit_factor<{gates.get('profit_factor_min')}")
    if float(summary.get("max_drawdown_pct", 0.0)) > float(gates.get("max_drawdown_pct_max", 1.0)):
        reasons.append(f"max_drawdown_pct>{gates.get('max_drawdown_pct_max')}")
    if int(summary.get("trades", 0)) < int(gates.get("trades_min", 0)):
        reasons.append(f"trades<{gates.get('trades_min')}")
    if float(summary.get("net_return_sum", 0.0)) <= float(gates.get("net_return_sum_min", 0.0)):
        reasons.append(f"net_return_sum<={gates.get('net_return_sum_min')}")
    side_share = float(summary.get("long_share", 0.0))
    if side_share < float(gates.get("side_share_min", 0.0)) or side_share > float(gates.get("side_share_max", 1.0)):
        reasons.append("side_share_out_of_band")
    if float(summary.get("block_rate", 0.0)) < float(gates.get("block_rate_min", 0.0)):
        reasons.append(f"block_rate<{gates.get('block_rate_min')}")
    return (not reasons), reasons


def _stage_component_ids(manifest: Dict[str, Any], stage_name: str) -> dict[str, str]:
    return {
        "view_id": str(manifest["views"][f"{stage_name}_view_id"]),
        "labeler_id": str(manifest["labels"][f"{stage_name}_labeler_id"]),
        "trainer_id": str(manifest["training"][f"{stage_name}_trainer_id"]),
        "policy_id": str(manifest["policy"][f"{stage_name}_policy_id"]),
    }


def validate_staged_research_environment(manifest: Dict[str, Any]) -> dict[str, Any]:
    catalog = dict(manifest.get("catalog") or {})
    models_by_stage = dict(catalog.get("models_by_stage") or {})
    resolved_models_by_stage: dict[str, list[str]] = {}
    stages: dict[str, dict[str, Any]] = {}
    for stage_name in STAGE_ORDER:
        resolution = ensure_requested_models_runnable(
            list(models_by_stage.get(stage_name) or []),
            context=f"catalog.models_by_stage.{stage_name}",
        )
        resolved_models_by_stage[stage_name] = list(resolution["runnable_models"])
        stages[stage_name] = {
            "requested_models": list(resolution["requested_models"]),
            "runnable_models": list(resolution["runnable_models"]),
            "unavailable_models": list(resolution["unavailable_models"]),
        }
    return {
        "stages": stages,
        "resolved_models_by_stage": resolved_models_by_stage,
    }


def run_staged_research(ctx: RunContext) -> Dict[str, Any]:
    manifest = dict(ctx.resolved_config)
    training_environment = validate_staged_research_environment(manifest)
    manifest["catalog"] = dict(manifest["catalog"])
    manifest["catalog"]["models_by_stage"] = {
        **dict(manifest["catalog"].get("models_by_stage") or {}),
        **{
            stage_name: list(model_names)
            for stage_name, model_names in dict(training_environment["resolved_models_by_stage"]).items()
        },
    }
    parquet_root = Path(manifest["inputs"]["parquet_root"]).resolve()
    support_dataset = str(manifest["inputs"]["support_dataset"])
    runtime_block_expiry = bool(manifest["runtime"].get("block_expiry", False))
    recipe_catalog = get_recipe_catalog(str(manifest["catalog"]["recipe_catalog_id"]))
    support_raw = _load_dataset(parquet_root, support_dataset)
    support_context = support_raw.loc[:, ~support_raw.columns.duplicated()].copy()
    support, support_filter_meta = _apply_runtime_filters(
        support_raw,
        block_expiry=runtime_block_expiry,
        context=f"staged support dataset {support_dataset}",
    )
    oracle, utility = _build_oracle_targets(support, recipe_catalog, cost_per_trade=float(manifest["training"]["cost_per_trade"]))
    support_windows = {
        "research_train": _window(support, manifest["windows"]["research_train"]),
        "research_valid": _window(support, manifest["windows"]["research_valid"]),
        "full_model": _window(support, manifest["windows"]["full_model"]),
        "final_holdout": _window(support, manifest["windows"]["final_holdout"]),
    }
    runtime_filtering: Dict[str, Any] = {
        "block_expiry": {
            "enabled": runtime_block_expiry,
            "support": {
                "dataset_name": support_dataset,
                **support_filter_meta,
            },
            "stages": {},
        }
    }
    label_filtering: Dict[str, Any] = {}

    components: dict[str, dict[str, str]] = {}
    stage_frames: dict[str, dict[str, pd.DataFrame]] = {}
    labeled_frames: dict[str, dict[str, pd.DataFrame]] = {}
    for stage_name in STAGE_ORDER:
        component_ids = _stage_component_ids(manifest, stage_name)
        components[stage_name] = component_ids
        dataset_name = view_registry()[component_ids["view_id"]].dataset_name
        stage_frame, stage_filter_meta = _apply_runtime_filters(
            _load_dataset(parquet_root, dataset_name),
            block_expiry=runtime_block_expiry,
            context=f"staged {stage_name} dataset {dataset_name}",
            support_context=support_context,
        )
        runtime_filtering["block_expiry"]["stages"][stage_name] = {
            "dataset_name": dataset_name,
            **stage_filter_meta,
        }
        labeler = resolve_labeler(component_ids["labeler_id"])
        labeled = labeler(stage_frame, oracle)
        if stage_name == "stage2":
            labeled, direction_filter_meta = _apply_stage2_label_filter(labeled, manifest)
            labeled, session_filter_meta = _apply_stage2_session_filter(labeled, manifest)
            label_filtering[stage_name] = {
                "direction_label_filter": direction_filter_meta,
                "session_filter": session_filter_meta,
            }
        stage_frames[stage_name] = {
            "research_train": _window(stage_frame, manifest["windows"]["research_train"]),
            "research_valid": _window(stage_frame, manifest["windows"]["research_valid"]),
            "full_model": _window(stage_frame, manifest["windows"]["full_model"]),
            "final_holdout": _window(stage_frame, manifest["windows"]["final_holdout"]),
        }
        labeled_frames[stage_name] = {
            "research_train": _window(labeled, manifest["windows"]["research_train"]),
            "research_valid": _window(labeled, manifest["windows"]["research_valid"]),
            "full_model": _window(labeled, manifest["windows"]["full_model"]),
            "final_holdout": _window(labeled, manifest["windows"]["final_holdout"]),
        }

    coverage_scenario_reports = _scenario_reports(
        base_frame=support_windows["final_holdout"],
        trade_rows=None,
        evaluation_mode="coverage_only",
    )

    cv_prechecks: dict[str, Any] = {
        "stage2_signal_check": _check_stage2_signal(labeled_frames["stage2"]["full_model"]),
        "stage1_cv": None,
        "stage2_cv": None,
    }
    if not bool(cv_prechecks["stage2_signal_check"]["has_signal"]):
        signal_reason = str(cv_prechecks["stage2_signal_check"].get("reason") or "signal_absent")
        return _early_hold_summary(
            ctx,
            manifest,
            components,
            blocking_reasons=[f"stage2_signal_check.{signal_reason}"],
            completed_stage_artifacts={},
            cv_prechecks=cv_prechecks,
            completion_mode="stage2_signal_check_failed",
            parquet_root=parquet_root,
            support_dataset=support_dataset,
            runtime_block_expiry=runtime_block_expiry,
            runtime_filtering=runtime_filtering,
            label_filtering=label_filtering,
            scenario_reports=coverage_scenario_reports,
            training_environment=dict(training_environment["stages"]),
        )

    stage_artifacts: dict[str, Any] = {}
    stage1_started_at = utc_now()
    ctx.append_state("stage_start", stage="stage1", started_at_utc=stage1_started_at)
    stage1_result = resolve_trainer(components["stage1"]["trainer_id"])(
        stage_name="stage1",
        train_frame=labeled_frames["stage1"]["research_train"],
        valid_frame=labeled_frames["stage1"]["research_valid"],
        full_model_frame=labeled_frames["stage1"]["full_model"],
        holdout_frame=labeled_frames["stage1"]["final_holdout"],
        policy_valid_frame=stage_frames["stage1"]["research_valid"],
        policy_holdout_frame=stage_frames["stage1"]["final_holdout"],
        manifest=manifest,
        models=list(manifest["catalog"]["models_by_stage"]["stage1"]),
        feature_sets=list(manifest["catalog"]["feature_sets_by_stage"]["stage1"]),
        label_mode="entry",
        positive_value=1,
        output_root=ctx.output_root / "stages",
        prob_col="entry_prob",
    )
    stage1_completed_at = utc_now()
    stage_artifacts["stage1"] = {
        "started_at_utc": stage1_started_at,
        "completed_at_utc": stage1_completed_at,
        "model_package_path": stage1_result["model_package_path"],
        "training_report_path": stage1_result["training_report_path"],
        "feature_contract_path": stage1_result["feature_contract_path"],
    }
    ctx.append_state("stage_done", stage="stage1", completed_at_utc=stage1_completed_at)
    stage1_cv_quality = _binary_quality(
        labeled_frames["stage1"]["research_valid"]["move_label"],
        stage1_result["validation_scores"]["entry_prob"],
    )
    stage1_cv_ok, stage1_cv_reasons = _stage_gate_result(
        stage1_cv_quality,
        dict(manifest["hard_gates"]["stage1"]),
        prefix="stage1_cv.",
    )
    cv_prechecks["stage1_cv"] = {
        **stage1_cv_quality,
        "gate_passed": stage1_cv_ok,
        "reasons": stage1_cv_reasons,
    }
    if not stage1_cv_ok:
        return _early_hold_summary(
            ctx,
            manifest,
            components,
            blocking_reasons=stage1_cv_reasons,
            completed_stage_artifacts=stage_artifacts,
            cv_prechecks=cv_prechecks,
            completion_mode="stage1_cv_gate_failed",
            parquet_root=parquet_root,
            support_dataset=support_dataset,
            runtime_block_expiry=runtime_block_expiry,
            runtime_filtering=runtime_filtering,
            label_filtering=label_filtering,
            scenario_reports=coverage_scenario_reports,
            training_environment=dict(training_environment["stages"]),
        )

    stage2_started_at = utc_now()
    ctx.append_state("stage_start", stage="stage2", started_at_utc=stage2_started_at)
    stage2_result = resolve_trainer(components["stage2"]["trainer_id"])(
        stage_name="stage2",
        train_frame=labeled_frames["stage2"]["research_train"],
        valid_frame=labeled_frames["stage2"]["research_valid"],
        full_model_frame=labeled_frames["stage2"]["full_model"],
        holdout_frame=labeled_frames["stage2"]["final_holdout"],
        policy_valid_frame=stage_frames["stage2"]["research_valid"],
        policy_holdout_frame=stage_frames["stage2"]["final_holdout"],
        manifest=manifest,
        models=list(manifest["catalog"]["models_by_stage"]["stage2"]),
        feature_sets=list(manifest["catalog"]["feature_sets_by_stage"]["stage2"]),
        label_mode="direction",
        positive_value="CE",
        output_root=ctx.output_root / "stages",
        prob_col="direction_up_prob",
    )
    stage2_completed_at = utc_now()
    stage_artifacts["stage2"] = {
        "started_at_utc": stage2_started_at,
        "completed_at_utc": stage2_completed_at,
        "model_package_path": stage2_result["model_package_path"],
        "training_report_path": stage2_result["training_report_path"],
        "feature_contract_path": stage2_result["feature_contract_path"],
    }
    ctx.append_state("stage_done", stage="stage2", completed_at_utc=stage2_completed_at)
    stage2_diagnostics = _build_stage2_diagnostics_report(
        ctx=ctx,
        manifest=manifest,
        labeled_frames=labeled_frames,
        stage2_result=stage2_result,
        label_filter_meta=dict(label_filtering.get("stage2") or {}),
    )
    stage2_diagnostics_path = ctx.write_json("stages/stage2/diagnostics.json", stage2_diagnostics)
    stage_artifacts["stage2"]["diagnostics_path"] = str(stage2_diagnostics_path.resolve())
    stage_artifacts["stage2"]["diagnostics_score_paths"] = dict(stage2_diagnostics.get("score_paths") or {})
    stage2_cv_labels = np.where(
        labeled_frames["stage2"]["research_valid"]["direction_label"].astype(str).str.upper() == "CE",
        1,
        0,
    )
    stage2_cv_quality = _binary_quality(
        pd.Series(stage2_cv_labels, index=labeled_frames["stage2"]["research_valid"].index),
        stage2_result["validation_scores"]["direction_up_prob"],
    )
    stage2_cv_ok, stage2_cv_reasons = _stage_gate_result(
        stage2_cv_quality,
        dict(manifest["hard_gates"]["stage2"]),
        prefix="stage2_cv.",
    )
    cv_prechecks["stage2_cv"] = {
        **stage2_cv_quality,
        "gate_passed": stage2_cv_ok,
        "reasons": stage2_cv_reasons,
    }
    if not stage2_cv_ok:
        return _early_hold_summary(
            ctx,
            manifest,
            components,
            blocking_reasons=stage2_cv_reasons,
            completed_stage_artifacts=stage_artifacts,
            cv_prechecks=cv_prechecks,
            completion_mode="stage2_cv_gate_failed",
            parquet_root=parquet_root,
            support_dataset=support_dataset,
            runtime_block_expiry=runtime_block_expiry,
            runtime_filtering=runtime_filtering,
            label_filtering=label_filtering,
            scenario_reports=coverage_scenario_reports,
            training_environment=dict(training_environment["stages"]),
        )

    stage3_started_at = utc_now()
    ctx.append_state("stage_start", stage="stage3", started_at_utc=stage3_started_at)
    stage3_result = resolve_trainer(components["stage3"]["trainer_id"])(
        stage_name="stage3",
        train_frame=_add_upstream_probs(
            labeled_frames["stage3"]["research_train"],
            stage1_source_frame=stage_frames["stage1"]["research_train"],
            stage2_source_frame=stage_frames["stage2"]["research_train"],
            stage1_package=stage1_result["search_package"],
            stage2_package=stage2_result["search_package"],
        ),
        valid_frame=_add_upstream_probs(
            stage_frames["stage3"]["research_valid"],
            stage1_source_frame=stage_frames["stage1"]["research_valid"],
            stage2_source_frame=stage_frames["stage2"]["research_valid"],
            stage1_package=stage1_result["search_package"],
            stage2_package=stage2_result["search_package"],
        ),
        full_model_frame=_add_upstream_probs(
            labeled_frames["stage3"]["full_model"],
            stage1_source_frame=stage_frames["stage1"]["full_model"],
            stage2_source_frame=stage_frames["stage2"]["full_model"],
            stage1_package=stage1_result["model_package"],
            stage2_package=stage2_result["model_package"],
        ),
        holdout_frame=_add_upstream_probs(
            stage_frames["stage3"]["final_holdout"],
            stage1_source_frame=stage_frames["stage1"]["final_holdout"],
            stage2_source_frame=stage_frames["stage2"]["final_holdout"],
            stage1_package=stage1_result["model_package"],
            stage2_package=stage2_result["model_package"],
        ),
        manifest=manifest,
        models=list(manifest["catalog"]["models_by_stage"]["stage3"]),
        feature_sets=list(manifest["catalog"]["feature_sets_by_stage"]["stage3"]),
        output_root=ctx.output_root / "stages",
    )
    stage3_completed_at = utc_now()
    stage_artifacts["stage3"] = {
        "started_at_utc": stage3_started_at,
        "completed_at_utc": stage3_completed_at,
        "training_report_path": stage3_result["training_report_path"],
        "recipes": sorted(stage3_result["recipe_packages"].keys()),
        "recipe_artifacts": dict(stage3_result["recipe_artifacts"]),
    }
    ctx.append_state("stage_done", stage="stage3", completed_at_utc=stage3_completed_at)

    utility_valid = _window(utility, manifest["windows"]["research_valid"])
    utility_holdout = _window(utility, manifest["windows"]["final_holdout"])
    stage1_policy_scores_valid = stage1_result["validation_policy_scores"]
    stage1_policy_scores_holdout = stage1_result["holdout_policy_scores"]
    stage2_policy_scores_valid = stage2_result["validation_policy_scores"]
    stage2_policy_scores_holdout = stage2_result["holdout_policy_scores"]
    stage3_policy_scores_valid = stage3_result["validation_policy_scores"]
    stage3_policy_scores_holdout = stage3_result["holdout_policy_scores"]
    stage1_policy = resolve_policy(components["stage1"]["policy_id"])(
        stage1_policy_scores_valid,
        utility_valid,
        dict(manifest["policy"]["stage1"]),
    )
    stage2_policy = resolve_policy(components["stage2"]["policy_id"])(
        stage2_policy_scores_valid,
        utility_valid,
        stage1_policy_scores_valid,
        stage1_policy,
        dict(manifest["policy"]["stage2"]),
    )
    stage3_policy = resolve_policy(components["stage3"]["policy_id"])(
        stage3_policy_scores_valid,
        utility_valid,
        stage1_policy_scores_valid,
        stage2_policy_scores_valid,
        stage1_policy,
        stage2_policy,
        dict(manifest["policy"]["stage3"]),
        [recipe.recipe_id for recipe in recipe_catalog],
    )

    stage1_holdout_quality = _binary_quality(labeled_frames["stage1"]["final_holdout"]["entry_label"], stage1_result["holdout_scores"]["entry_prob"])
    stage2_holdout_quality = _binary_quality(np.where(labeled_frames["stage2"]["final_holdout"]["direction_label"].astype(str).str.upper() == "CE", 1, 0), stage2_result["holdout_scores"]["direction_up_prob"])
    combined_holdout_summary = _evaluate_combined_policy(
        utility_holdout,
        stage1_policy_scores_holdout,
        stage2_policy_scores_holdout,
        stage3_policy_scores_holdout,
        stage1_threshold=float(stage1_policy["selected_threshold"]),
        ce_threshold=float(stage2_policy["selected_ce_threshold"]),
        pe_threshold=float(stage2_policy["selected_pe_threshold"]),
        min_edge=float(stage2_policy["selected_min_edge"]),
        recipe_threshold=float(stage3_policy["selected_threshold"]),
        recipe_margin_min=float(stage3_policy["selected_margin_min"]),
        recipe_ids=[recipe.recipe_id for recipe in recipe_catalog],
    )
    combined_trade_rows = _combined_policy_trade_rows(
        stage_frames["stage3"]["final_holdout"],
        utility_holdout,
        stage1_policy_scores_holdout,
        stage2_policy_scores_holdout,
        stage3_policy_scores_holdout,
        stage1_threshold=float(stage1_policy["selected_threshold"]),
        ce_threshold=float(stage2_policy["selected_ce_threshold"]),
        pe_threshold=float(stage2_policy["selected_pe_threshold"]),
        min_edge=float(stage2_policy["selected_min_edge"]),
        recipe_threshold=float(stage3_policy["selected_threshold"]),
        recipe_margin_min=float(stage3_policy["selected_margin_min"]),
        recipe_ids=[recipe.recipe_id for recipe in recipe_catalog],
    )
    combined_trade_rows = _attach_support_context(combined_trade_rows, support_windows["final_holdout"])
    training_regime_distribution = _distribution_from_series(_regime_label_series(combined_trade_rows))
    scenario_reports = _scenario_reports(
        base_frame=support_windows["final_holdout"],
        trade_rows=combined_trade_rows,
        evaluation_mode="combined_policy_holdout",
    )
    fixed_recipe_baselines = [
        _fixed_recipe_baseline(
            utility_holdout,
            stage1_policy_scores_holdout,
            stage2_policy_scores_holdout,
            stage1_threshold=float(stage1_policy["selected_threshold"]),
            ce_threshold=float(stage2_policy["selected_ce_threshold"]),
            pe_threshold=float(stage2_policy["selected_pe_threshold"]),
            min_edge=float(stage2_policy["selected_min_edge"]),
            recipe_id=recipe.recipe_id,
        )
        for recipe in recipe_catalog
    ]
    best_fixed_baseline = max(fixed_recipe_baselines, key=lambda row: (float(row["net_return_sum"]), float(row["profit_factor"]), -float(row["max_drawdown_pct"])))

    stage1_gate_ok, stage1_gate_reasons = _stage_gate_result(stage1_holdout_quality, dict(manifest["hard_gates"]["stage1"]), prefix="stage1.")
    stage2_gate_ok, stage2_gate_reasons = _stage_gate_result(stage2_holdout_quality, dict(manifest["hard_gates"]["stage2"]), prefix="stage2.")
    stage3_gate_ok = (
        float(combined_holdout_summary["net_return_sum"]) >= float(best_fixed_baseline["net_return_sum"])
        and float(combined_holdout_summary["profit_factor"]) >= float(best_fixed_baseline["profit_factor"])
        and float(combined_holdout_summary["max_drawdown_pct"]) <= float(best_fixed_baseline["max_drawdown_pct"]) + float(dict(manifest["hard_gates"]["stage3"]).get("max_drawdown_slack", 0.01))
    )
    stage3_gate_reasons = [] if stage3_gate_ok else ["stage3.non_inferior_to_fixed_recipe_baseline_failed"]
    combined_gate_ok, combined_gate_reasons = _combined_gate_result(combined_holdout_summary, dict(manifest["hard_gates"]["combined"]))
    blocking_reasons = stage1_gate_reasons + stage2_gate_reasons + stage3_gate_reasons + combined_gate_reasons
    publish_assessment = {"decision": ("PUBLISH" if not blocking_reasons else "HOLD"), "publishable": not blocking_reasons, "blocking_reasons": blocking_reasons}

    summary = {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at_utc": utc_now(),
        "status": "completed",
        "experiment_kind": str(manifest["experiment_kind"]),
        "run_id": str(ctx.output_root.name),
        "parquet_root": str(parquet_root),
        "support_dataset": support_dataset,
        "recipe_catalog_id": str(manifest["catalog"]["recipe_catalog_id"]),
        "component_ids": components,
        "completion_mode": "completed",
        "cv_prechecks": cv_prechecks,
        "training_regime_distribution": training_regime_distribution,
        "policy_reports": {"stage1": stage1_policy, "stage2": stage2_policy, "stage3": stage3_policy},
        "holdout_reports": {
            "stage1": stage1_holdout_quality,
            "stage2": stage2_holdout_quality,
            "stage3": {"combined_holdout_summary": combined_holdout_summary, "best_fixed_recipe_baseline": best_fixed_baseline, "all_fixed_recipe_baselines": fixed_recipe_baselines},
        },
        "gates": {
            "stage1": {"passed": stage1_gate_ok, "reasons": stage1_gate_reasons},
            "stage2": {"passed": stage2_gate_ok, "reasons": stage2_gate_reasons},
            "stage3": {"passed": stage3_gate_ok, "reasons": stage3_gate_reasons},
            "combined": {"passed": combined_gate_ok, "reasons": combined_gate_reasons},
        },
        "publish_assessment": publish_assessment,
        "runtime_prefilter_gate_ids": list(manifest["runtime"]["prefilter_gate_ids"]),
        "runtime_block_expiry": runtime_block_expiry,
        "runtime_filtering": runtime_filtering,
        "label_filtering": label_filtering,
        "scenario_reports": scenario_reports,
        "training_environment": dict(training_environment["stages"]),
        "stage_artifacts": stage_artifacts,
    }
    ctx.write_json("summary.json", summary)
    return summary


__all__ = [
    "build_stage1_labels",
    "build_stage2_labels",
    "build_stage3_labels",
    "run_staged_research",
    "select_direction_policy",
    "select_entry_policy",
    "select_recipe_policy",
    "train_binary_catalog_stage",
    "train_recipe_ovr_stage",
    "validate_staged_research_environment",
]
