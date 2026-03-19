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
from ..experiment_control.state import RunContext, utc_now
from ..inference_contract.predict import predict_probabilities_from_frame
from ..labeling import EffectiveLabelConfig, build_labeled_dataset, prepare_snapshot_labeled_frame
from ..model_search import run_training_cycle_catalog
from ..model_search.metrics import max_drawdown_pct, profit_factor
from .recipes import get_recipe_catalog
from .registries import resolve_labeler, resolve_policy, resolve_trainer, view_registry

KEY_COLUMNS = ["trade_date", "timestamp", "snapshot_id"]
STAGE_ORDER = ("stage1", "stage2", "stage3")
SUMMARY_SCHEMA_VERSION = 2


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
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
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
    expiry_mask, signal_column = _expiry_day_mask(out, context=context)
    meta["signal_column"] = signal_column
    meta["expiry_rows_dropped"] = int(expiry_mask.sum())
    out = out.loc[~expiry_mask].copy().sort_values("timestamp").reset_index(drop=True)
    meta["rows_after"] = int(len(out))
    return out, meta


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
    labeled["timestamp"] = pd.to_datetime(labeled["timestamp"], errors="coerce")
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


def _build_oracle_targets(
    support: pd.DataFrame,
    recipes: Sequence[LabelRecipe],
    *,
    cost_per_trade: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if bool(support.duplicated(subset=KEY_COLUMNS).any()):
        raise ValueError("support frame contains duplicate staged oracle keys")
    utility = support.loc[:, KEY_COLUMNS].copy()
    recipe_rows_by_key: dict[str, dict[tuple[str, pd.Timestamp, str], dict[str, Any]]] = {}
    for recipe in recipes:
        labeled = _align_recipe_frame(support, _label_recipe_frame(support, recipe), recipe_id=recipe.recipe_id)
        recipe_rows_by_key[recipe.recipe_id] = {
            (
                str(row["trade_date"]),
                pd.Timestamp(row["timestamp"]),
                str(row["snapshot_id"]),
            ): dict(row)
            for row in labeled.to_dict(orient="records")
        }
        utility[f"{recipe.recipe_id}__ce_net_return"] = labeled.apply(
            lambda row: _path_return(row, prefix="ce") - float(cost_per_trade),
            axis=1,
        )
        utility[f"{recipe.recipe_id}__pe_net_return"] = labeled.apply(
            lambda row: _path_return(row, prefix="pe") - float(cost_per_trade),
            axis=1,
        )

    best_ce_cols = [f"{recipe.recipe_id}__ce_net_return" for recipe in recipes]
    best_pe_cols = [f"{recipe.recipe_id}__pe_net_return" for recipe in recipes]
    utility["best_ce_net_return_after_cost"] = utility[best_ce_cols].max(axis=1)
    utility["best_pe_net_return_after_cost"] = utility[best_pe_cols].max(axis=1)
    utility["best_available_net_return_after_cost"] = utility[
        ["best_ce_net_return_after_cost", "best_pe_net_return_after_cost"]
    ].max(axis=1)
    utility_by_key = {
        (
            str(row["trade_date"]),
            pd.Timestamp(row["timestamp"]),
            str(row["snapshot_id"]),
        ): dict(row)
        for row in utility.to_dict(orient="records")
    }

    oracle_rows: list[dict[str, Any]] = []
    for support_row in support.to_dict(orient="records"):
        key = (
            str(support_row["trade_date"]),
            pd.Timestamp(support_row["timestamp"]),
            str(support_row["snapshot_id"]),
        )
        utility_row = utility_by_key[key]
        best: Optional[dict[str, Any]] = None
        for recipe in recipes:
            row = recipe_rows_by_key[recipe.recipe_id][key]
            for side, prefix, direction_up in (("CE", "ce", 1), ("PE", "pe", 0)):
                valid = _safe_float(row.get(f"{prefix}_label_valid"), default=0.0)
                net = _safe_float(utility_row[f"{recipe.recipe_id}__{prefix}_net_return"])
                if valid != 1.0 or (not np.isfinite(net)) or net <= 0.0:
                    continue
                candidate = {
                    "recipe_id": recipe.recipe_id,
                    "side": side,
                    "direction_up": int(direction_up),
                    "net_return_after_cost": float(net),
                    "adverse_excursion": float(_adverse_excursion(row, prefix=prefix)),
                    "horizon_minutes": int(recipe.horizon_minutes),
                    "stop_loss_pct": float(recipe.stop_loss_pct),
                    "take_profit_pct": float(recipe.take_profit_pct),
                }
                if _candidate_better(candidate, best):
                    best = candidate
        oracle_rows.append(
            {
                "trade_date": str(support_row["trade_date"]),
                "timestamp": pd.Timestamp(support_row["timestamp"]),
                "snapshot_id": str(support_row["snapshot_id"]),
                "entry_label": int(best is not None),
                "direction_label": (str(best["side"]) if best is not None else None),
                "direction_up": (int(best["direction_up"]) if best is not None else None),
                "recipe_label": (str(best["recipe_id"]) if best is not None else None),
                "best_net_return_after_cost": (
                    float(best["net_return_after_cost"])
                    if best is not None
                    else float(utility_row["best_available_net_return_after_cost"])
                ),
            }
        )
    oracle = pd.DataFrame(oracle_rows)
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
    )


def _selected_model_name(search_payload: Dict[str, Any]) -> str:
    best = dict(search_payload["report"]["best_experiment"])
    model_meta = best.get("model")
    if not isinstance(model_meta, dict):
        raise ValueError("best_experiment missing model metadata")
    return str(model_meta.get("name") or "").strip()


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
) -> dict[str, Any]:
    training_cfg = dict(manifest["training"])
    preprocess = PreprocessConfig(**dict(training_cfg["preprocess"]))
    label_col = "entry_label" if label_mode == "entry" else "direction_label"
    objective = str(training_cfg["objectives_by_stage"][stage_name])
    label_target = "move_barrier_hit" if label_mode == "entry" else "move_direction_up"
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
    )
    best_feature_set = str(search_payload["report"]["best_experiment"]["feature_set"])
    best_model_name = _selected_model_name(search_payload)
    final_payload = _training_call(
        _stage_binary_frame(full_model_frame, mode=label_mode, label_col=label_col, positive_value=positive_value),
        objective=objective,
        label_target=label_target,
        models=[best_model_name],
        feature_sets=[best_feature_set],
        preprocess=preprocess,
        cv_config=dict(training_cfg["cv_config"]),
        random_state=int(training_cfg["random_state"]),
        model_n_jobs=int(training_cfg["runtime"]["model_n_jobs"]),
    )

    search_package = dict(search_payload["model_package"])
    final_package = dict(final_payload["model_package"])
    valid_scores = _score_single_target(valid_frame, search_package, prob_col=prob_col)
    holdout_scores = _score_single_target(holdout_frame, final_package, prob_col=prob_col)

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
    }


def _merge_score_frames(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=KEY_COLUMNS)
    out = frames[0].copy()
    for frame in frames[1:]:
        out = out.merge(frame, on=KEY_COLUMNS, how="outer")
    return out.sort_values("timestamp").reset_index(drop=True)


def _add_upstream_probs(
    frame: pd.DataFrame,
    *,
    stage1_package: Dict[str, Any],
    stage2_package: Dict[str, Any],
) -> pd.DataFrame:
    out = frame.copy()
    stage1_scores = _score_single_target(out, stage1_package, prob_col="stage1_entry_prob")
    stage2_scores = _score_single_target(out, stage2_package, prob_col="stage2_direction_up_prob")
    out = out.merge(stage1_scores, on=KEY_COLUMNS, how="left")
    out = out.merge(stage2_scores, on=KEY_COLUMNS, how="left")
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
) -> dict[str, Any]:
    training_cfg = dict(manifest["training"])
    preprocess = PreprocessConfig(**dict(training_cfg["preprocess"]))
    objective = str(training_cfg["objectives_by_stage"][stage_name])
    recipe_ids = [recipe.recipe_id for recipe in get_recipe_catalog(str(manifest["catalog"]["recipe_catalog_id"]))]
    stage_root = output_root / stage_name
    stage_root.mkdir(parents=True, exist_ok=True)

    recipe_payloads: dict[str, Dict[str, Any]] = {}
    valid_frames: list[pd.DataFrame] = []
    holdout_frames: list[pd.DataFrame] = []
    recipe_reports: dict[str, Any] = {}
    for recipe_id in recipe_ids:
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
            model_n_jobs=int(training_cfg["runtime"]["model_n_jobs"]),
        )
        best_feature_set = str(search_payload["report"]["best_experiment"]["feature_set"])
        best_model_name = _selected_model_name(search_payload)
        final_payload = _training_call(
            _stage_binary_frame(full_model_frame, mode="recipe", label_col="recipe_label", positive_value=recipe_id),
            objective=objective,
            label_target="move_barrier_hit",
            models=[best_model_name],
            feature_sets=[best_feature_set],
            preprocess=preprocess,
            cv_config=dict(training_cfg["cv_config"]),
            random_state=int(training_cfg["random_state"]),
            model_n_jobs=int(training_cfg["runtime"]["model_n_jobs"]),
        )
        search_package = dict(search_payload["model_package"])
        final_package = dict(final_payload["model_package"])
        prob_col = f"recipe_prob_{recipe_id}"
        valid_frames.append(_score_single_target(valid_frame, search_package, prob_col=prob_col))
        holdout_frames.append(_score_single_target(holdout_frame, final_package, prob_col=prob_col))
        joblib.dump(final_package, recipe_root / "model.joblib")
        (recipe_root / "training_report.json").write_text(json.dumps(final_payload["report"], indent=2), encoding="utf-8")
        (recipe_root / "feature_contract.json").write_text(
            json.dumps(dict(final_package.get("_model_input_contract") or {}), indent=2),
            encoding="utf-8",
        )
        recipe_payloads[recipe_id] = final_package
        recipe_reports[recipe_id] = {
            "model_package_path": str((recipe_root / "model.joblib").resolve()),
            "training_report_path": str((recipe_root / "training_report.json").resolve()),
            "feature_contract_path": str((recipe_root / "feature_contract.json").resolve()),
        }
    summary = {
        "recipe_ids": recipe_ids,
        "reports": recipe_reports,
    }
    (stage_root / "training_report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return {
        "stage_name": stage_name,
        "recipe_packages": recipe_payloads,
        "recipe_artifacts": recipe_reports,
        "training_report_path": str((stage_root / "training_report.json").resolve()),
        "validation_scores": _merge_score_frames(valid_frames),
        "holdout_scores": _merge_score_frames(holdout_frames),
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


def select_entry_policy(valid_scores: pd.DataFrame, utility: pd.DataFrame, policy_config: Dict[str, Any]) -> dict[str, Any]:
    merged = valid_scores.merge(utility, on=KEY_COLUMNS, how="inner")
    rows = []
    for threshold in list(policy_config.get("threshold_grid") or []):
        mask = pd.to_numeric(merged["entry_prob"], errors="coerce").fillna(0.0) >= float(threshold)
        returns = pd.to_numeric(merged.loc[mask, "best_available_net_return_after_cost"], errors="coerce").fillna(0.0).tolist()
        summary = _summarize_returns(returns, rows_total=len(merged))
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
    merged = valid_scores.merge(stage1_scores, on=KEY_COLUMNS, how="inner").merge(utility, on=KEY_COLUMNS, how="inner")
    entry_threshold = float(stage1_policy["selected_threshold"])
    rows = []
    for ce_threshold in list(policy_config.get("ce_threshold_grid") or []):
        for pe_threshold in list(policy_config.get("pe_threshold_grid") or []):
            for min_edge in list(policy_config.get("min_edge_grid") or []):
                returns: list[float] = []
                sides: list[str] = []
                for row in merged.itertuples(index=False):
                    data = row._asdict()
                    if _safe_float(data.get("entry_prob"), default=0.0) < entry_threshold:
                        continue
                    side = _choose_side(
                        _safe_float(data.get("direction_up_prob"), default=0.0),
                        ce_threshold=float(ce_threshold),
                        pe_threshold=float(pe_threshold),
                        min_edge=float(min_edge),
                    )
                    if side is None:
                        continue
                    returns.append(_safe_float(data.get("best_ce_net_return_after_cost" if side == "CE" else "best_pe_net_return_after_cost"), default=0.0))
                    sides.append(side)
                summary = _summarize_returns(returns, rows_total=len(merged), sides=sides)
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
    merged = utility.merge(stage1_scores, on=KEY_COLUMNS, how="inner")
    merged = merged.merge(stage2_scores, on=KEY_COLUMNS, how="inner")
    merged = merged.merge(stage3_scores, on=KEY_COLUMNS, how="inner")
    returns: list[float] = []
    sides: list[str] = []
    recipes: list[str] = []
    for row in merged.itertuples(index=False):
        data = row._asdict()
        if _safe_float(data.get("entry_prob"), default=0.0) < float(stage1_threshold):
            continue
        side = _choose_side(
            _safe_float(data.get("direction_up_prob"), default=0.0),
            ce_threshold=float(ce_threshold),
            pe_threshold=float(pe_threshold),
            min_edge=float(min_edge),
        )
        if side is None:
            continue
        recipe_id = _choose_recipe(
            data,
            recipe_ids,
            threshold=float(recipe_threshold),
            margin_min=float(recipe_margin_min),
        )
        if recipe_id is None:
            continue
        column = f"{recipe_id}__{side.lower()}_net_return"
        returns.append(_safe_float(data.get(column), default=0.0))
        sides.append(side)
        recipes.append(recipe_id)
    summary = _summarize_returns(returns, rows_total=len(merged), sides=sides, selected_recipes=recipes)
    summary["recipe_threshold"] = float(recipe_threshold)
    summary["recipe_margin_min"] = float(recipe_margin_min)
    return summary


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
    merged = utility.merge(stage1_scores, on=KEY_COLUMNS, how="inner").merge(stage2_scores, on=KEY_COLUMNS, how="inner")
    returns: list[float] = []
    sides: list[str] = []
    for row in merged.itertuples(index=False):
        data = row._asdict()
        if _safe_float(data.get("entry_prob"), default=0.0) < float(stage1_threshold):
            continue
        side = _choose_side(
            _safe_float(data.get("direction_up_prob"), default=0.0),
            ce_threshold=float(ce_threshold),
            pe_threshold=float(pe_threshold),
            min_edge=float(min_edge),
        )
        if side is None:
            continue
        returns.append(_safe_float(data.get(f"{recipe_id}__{side.lower()}_net_return"), default=0.0))
        sides.append(side)
    summary = _summarize_returns(returns, rows_total=len(merged), sides=sides, selected_recipes=[recipe_id] * len(returns))
    summary["recipe_id"] = str(recipe_id)
    return summary


def _stage_gate_result(quality: dict[str, Any], gates: dict[str, Any], *, prefix: str = "") -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if gates.get("roc_auc_min") is not None and quality.get("roc_auc") is not None and float(quality["roc_auc"]) < float(gates["roc_auc_min"]):
        reasons.append(f"{prefix}roc_auc<{gates['roc_auc_min']}")
    if gates.get("brier_max") is not None and quality.get("brier") is not None and float(quality["brier"]) > float(gates["brier_max"]):
        reasons.append(f"{prefix}brier>{gates['brier_max']}")
    if gates.get("roc_auc_drift_half_split_max_abs") is not None and quality.get("roc_auc_drift_half_split") is not None and float(quality["roc_auc_drift_half_split"]) > float(gates["roc_auc_drift_half_split_max_abs"]):
        reasons.append(f"{prefix}roc_auc_drift>{gates['roc_auc_drift_half_split_max_abs']}")
    return (not reasons), reasons


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


def run_staged_research(ctx: RunContext) -> Dict[str, Any]:
    manifest = dict(ctx.resolved_config)
    parquet_root = Path(manifest["inputs"]["parquet_root"]).resolve()
    support_dataset = str(manifest["inputs"]["support_dataset"])
    runtime_block_expiry = bool(manifest["runtime"].get("block_expiry", False))
    recipe_catalog = get_recipe_catalog(str(manifest["catalog"]["recipe_catalog_id"]))
    support, support_filter_meta = _apply_runtime_filters(
        _load_dataset(parquet_root, support_dataset),
        block_expiry=runtime_block_expiry,
        context=f"staged support dataset {support_dataset}",
    )
    oracle, utility = _build_oracle_targets(support, recipe_catalog, cost_per_trade=float(manifest["training"]["cost_per_trade"]))
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

    components: dict[str, dict[str, str]] = {}
    labeled_frames: dict[str, dict[str, pd.DataFrame]] = {}
    for stage_name in STAGE_ORDER:
        component_ids = _stage_component_ids(manifest, stage_name)
        components[stage_name] = component_ids
        dataset_name = view_registry()[component_ids["view_id"]].dataset_name
        stage_frame, stage_filter_meta = _apply_runtime_filters(
            _load_dataset(parquet_root, dataset_name),
            block_expiry=runtime_block_expiry,
            context=f"staged {stage_name} dataset {dataset_name}",
        )
        runtime_filtering["block_expiry"]["stages"][stage_name] = {
            "dataset_name": dataset_name,
            **stage_filter_meta,
        }
        labeler = resolve_labeler(component_ids["labeler_id"])
        labeled = labeler(stage_frame, oracle)
        labeled_frames[stage_name] = {
            "research_train": _window(labeled, manifest["windows"]["research_train"]),
            "research_valid": _window(labeled, manifest["windows"]["research_valid"]),
            "full_model": _window(labeled, manifest["windows"]["full_model"]),
            "final_holdout": _window(labeled, manifest["windows"]["final_holdout"]),
        }

    stage1_started_at = utc_now()
    stage1_result = resolve_trainer(components["stage1"]["trainer_id"])(
        stage_name="stage1",
        train_frame=labeled_frames["stage1"]["research_train"],
        valid_frame=labeled_frames["stage1"]["research_valid"],
        full_model_frame=labeled_frames["stage1"]["full_model"],
        holdout_frame=labeled_frames["stage1"]["final_holdout"],
        manifest=manifest,
        models=list(manifest["catalog"]["models_by_stage"]["stage1"]),
        feature_sets=list(manifest["catalog"]["feature_sets_by_stage"]["stage1"]),
        label_mode="entry",
        positive_value=1,
        output_root=ctx.output_root / "stages",
        prob_col="entry_prob",
    )
    stage1_completed_at = utc_now()
    stage2_started_at = utc_now()
    stage2_result = resolve_trainer(components["stage2"]["trainer_id"])(
        stage_name="stage2",
        train_frame=labeled_frames["stage2"]["research_train"],
        valid_frame=labeled_frames["stage2"]["research_valid"],
        full_model_frame=labeled_frames["stage2"]["full_model"],
        holdout_frame=labeled_frames["stage2"]["final_holdout"],
        manifest=manifest,
        models=list(manifest["catalog"]["models_by_stage"]["stage2"]),
        feature_sets=list(manifest["catalog"]["feature_sets_by_stage"]["stage2"]),
        label_mode="direction",
        positive_value="CE",
        output_root=ctx.output_root / "stages",
        prob_col="direction_up_prob",
    )
    stage2_completed_at = utc_now()
    stage3_started_at = utc_now()
    stage3_result = resolve_trainer(components["stage3"]["trainer_id"])(
        stage_name="stage3",
        train_frame=_add_upstream_probs(labeled_frames["stage3"]["research_train"], stage1_package=stage1_result["search_package"], stage2_package=stage2_result["search_package"]),
        valid_frame=_add_upstream_probs(labeled_frames["stage3"]["research_valid"], stage1_package=stage1_result["search_package"], stage2_package=stage2_result["search_package"]),
        full_model_frame=_add_upstream_probs(labeled_frames["stage3"]["full_model"], stage1_package=stage1_result["model_package"], stage2_package=stage2_result["model_package"]),
        holdout_frame=_add_upstream_probs(labeled_frames["stage3"]["final_holdout"], stage1_package=stage1_result["model_package"], stage2_package=stage2_result["model_package"]),
        manifest=manifest,
        models=list(manifest["catalog"]["models_by_stage"]["stage3"]),
        feature_sets=list(manifest["catalog"]["feature_sets_by_stage"]["stage3"]),
        output_root=ctx.output_root / "stages",
    )
    stage3_completed_at = utc_now()

    utility_valid = _window(utility, manifest["windows"]["research_valid"])
    utility_holdout = _window(utility, manifest["windows"]["final_holdout"])
    stage1_policy = resolve_policy(components["stage1"]["policy_id"])(stage1_result["validation_scores"], utility_valid, dict(manifest["policy"]["stage1"]))
    stage2_policy = resolve_policy(components["stage2"]["policy_id"])(stage2_result["validation_scores"], utility_valid, stage1_result["validation_scores"], stage1_policy, dict(manifest["policy"]["stage2"]))
    stage3_policy = resolve_policy(components["stage3"]["policy_id"])(stage3_result["validation_scores"], utility_valid, stage1_result["validation_scores"], stage2_result["validation_scores"], stage1_policy, stage2_policy, dict(manifest["policy"]["stage3"]), [recipe.recipe_id for recipe in recipe_catalog])

    stage1_holdout_quality = _binary_quality(labeled_frames["stage1"]["final_holdout"]["entry_label"], stage1_result["holdout_scores"]["entry_prob"])
    stage2_holdout_quality = _binary_quality(np.where(labeled_frames["stage2"]["final_holdout"]["direction_label"].astype(str).str.upper() == "CE", 1, 0), stage2_result["holdout_scores"]["direction_up_prob"])
    combined_holdout_summary = _evaluate_combined_policy(
        utility_holdout,
        stage1_result["holdout_scores"],
        stage2_result["holdout_scores"],
        stage3_result["holdout_scores"],
        stage1_threshold=float(stage1_policy["selected_threshold"]),
        ce_threshold=float(stage2_policy["selected_ce_threshold"]),
        pe_threshold=float(stage2_policy["selected_pe_threshold"]),
        min_edge=float(stage2_policy["selected_min_edge"]),
        recipe_threshold=float(stage3_policy["selected_threshold"]),
        recipe_margin_min=float(stage3_policy["selected_margin_min"]),
        recipe_ids=[recipe.recipe_id for recipe in recipe_catalog],
    )
    fixed_recipe_baselines = [
        _fixed_recipe_baseline(
            utility_holdout,
            stage1_result["holdout_scores"],
            stage2_result["holdout_scores"],
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
        "stage_artifacts": {
            "stage1": {
                "started_at_utc": stage1_started_at,
                "completed_at_utc": stage1_completed_at,
                "model_package_path": stage1_result["model_package_path"],
                "training_report_path": stage1_result["training_report_path"],
                "feature_contract_path": stage1_result["feature_contract_path"],
            },
            "stage2": {
                "started_at_utc": stage2_started_at,
                "completed_at_utc": stage2_completed_at,
                "model_package_path": stage2_result["model_package_path"],
                "training_report_path": stage2_result["training_report_path"],
                "feature_contract_path": stage2_result["feature_contract_path"],
            },
            "stage3": {
                "started_at_utc": stage3_started_at,
                "completed_at_utc": stage3_completed_at,
                "training_report_path": stage3_result["training_report_path"],
                "recipes": sorted(stage3_result["recipe_packages"].keys()),
                "recipe_artifacts": dict(stage3_result["recipe_artifacts"]),
            },
        },
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
]
