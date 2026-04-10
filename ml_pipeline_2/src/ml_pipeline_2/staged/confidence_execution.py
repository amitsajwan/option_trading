from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Sequence

import joblib
import numpy as np
import pandas as pd

from ..contracts.types import LabelRecipe
from ..experiment_control.state import utc_now
from .counterfactual import _load_json, _normalize_top_fractions, _resolve_recipe_universe, _summary_with_recipe
from .pipeline import (
    _apply_policy_soft_preferences,
    _apply_runtime_filters,
    _build_oracle_targets,
    _economic_balance_rank,
    _load_dataset,
    _merge_policy_inputs,
    _safe_float,
    _score_single_target,
    _score_stage2_package,
    _stage2_side_masks_from_policy,
    _window,
)
from .registries import view_registry


DEFAULT_CONFIDENCE_EXECUTION_TOP_FRACTIONS: tuple[float, ...] = (1.0, 0.5, 1.0 / 3.0, 0.25, 0.10)
DEFAULT_CONFIDENCE_EXECUTION_FIXED_RECIPE_IDS: tuple[str, ...] = ("L3", "L6")
DEFAULT_CONFIDENCE_EXECUTION_POLICY: dict[str, Any] = {
    "validation_min_trades_soft": 50,
    "side_share_min": 0.30,
    "side_share_max": 0.70,
    "prefer_non_negative_returns": True,
    "prefer_profit_factor_min": 1.0,
}


def _candidate_keep_count(total: int, fraction: float) -> int:
    if total <= 0:
        return 0
    if float(fraction) >= 1.0:
        return int(total)
    return max(1, int(math.ceil(total * float(fraction))))


def _selected_stage12_trades_for_window(
    *,
    resolved_config: Dict[str, Any],
    summary: Dict[str, Any],
    window_name: str,
    utility_window: pd.DataFrame,
    support_context: pd.DataFrame,
    parquet_root: Path,
    runtime_block_expiry: bool,
    stage1_package: Dict[str, Any],
    stage2_package: Dict[str, Any],
) -> pd.DataFrame:
    component_ids = dict(summary.get("component_ids") or {})
    stage1_view_id = str(((component_ids.get("stage1") or {}).get("view_id")) or "")
    stage2_view_id = str(((component_ids.get("stage2") or {}).get("view_id")) or "")
    if not stage1_view_id or not stage2_view_id:
        raise ValueError("summary component_ids must include stage1/stage2 view ids")

    stage1_dataset_name = view_registry()[stage1_view_id].dataset_name
    stage2_dataset_name = view_registry()[stage2_view_id].dataset_name
    stage1_frame_raw = _load_dataset(parquet_root, stage1_dataset_name)
    stage2_frame_raw = _load_dataset(parquet_root, stage2_dataset_name)
    stage1_frame, _ = _apply_runtime_filters(
        stage1_frame_raw,
        block_expiry=runtime_block_expiry,
        context=f"confidence execution {window_name} stage1 dataset {stage1_dataset_name}",
        support_context=support_context,
    )
    stage2_frame, _ = _apply_runtime_filters(
        stage2_frame_raw,
        block_expiry=runtime_block_expiry,
        context=f"confidence execution {window_name} stage2 dataset {stage2_dataset_name}",
        support_context=support_context,
    )
    window = dict((resolved_config.get("windows") or {}).get(window_name) or {})
    stage1_window = _window(stage1_frame, window)
    stage2_window = _window(stage2_frame, window)

    stage1_scores = _score_single_target(stage1_window, stage1_package, prob_col="entry_prob")
    stage2_scores = _score_stage2_package(stage2_window, stage2_package)
    stage1_policy = dict((summary.get("policy_reports") or {}).get("stage1") or {})
    stage2_policy = dict((summary.get("policy_reports") or {}).get("stage2") or {})
    if "selected_threshold" not in stage1_policy:
        raise ValueError("stage1 selected_threshold missing from summary policy_reports")

    merged = _merge_policy_inputs(utility_window, stage1_scores, stage2_scores)
    direction_available = pd.to_numeric(merged["direction_up_prob"], errors="coerce").notna().to_numpy(dtype=bool, copy=False)
    ce_mask, pe_mask = _stage2_side_masks_from_policy(
        merged,
        entry_threshold=float(stage1_policy["selected_threshold"]),
        stage2_policy=stage2_policy,
    )
    ce_mask = ce_mask & direction_available
    pe_mask = pe_mask & direction_available
    trade_mask = ce_mask | pe_mask
    selected = merged.loc[trade_mask].copy()
    if len(selected) == 0:
        return selected

    selected["selected_side"] = np.where(ce_mask[trade_mask], "CE", "PE")
    selected["entry_prob"] = pd.to_numeric(selected["entry_prob"], errors="coerce").fillna(0.0)
    if "direction_trade_prob" in selected.columns:
        selected["direction_trade_prob"] = pd.to_numeric(selected["direction_trade_prob"], errors="coerce").fillna(1.0)
    else:
        selected["direction_trade_prob"] = 1.0
    selected["direction_up_prob"] = pd.to_numeric(selected["direction_up_prob"], errors="coerce").fillna(0.5)
    selected["selected_side_prob"] = np.where(
        selected["selected_side"].eq("CE"),
        selected["direction_up_prob"],
        1.0 - selected["direction_up_prob"],
    )
    selected["ranking_score"] = (
        selected["entry_prob"]
        * selected["direction_trade_prob"]
        * selected["selected_side_prob"]
    )
    selected["oracle_selected_side_return"] = np.where(
        selected["selected_side"].eq("CE"),
        pd.to_numeric(selected["best_ce_net_return_after_cost"], errors="coerce").fillna(0.0),
        pd.to_numeric(selected["best_pe_net_return_after_cost"], errors="coerce").fillna(0.0),
    )
    selected = selected.sort_values(
        by=["ranking_score", "selected_side_prob", "entry_prob", "timestamp"],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)
    return selected


def _attach_recipe_selected_returns(frame: pd.DataFrame, recipes: Sequence[LabelRecipe]) -> pd.DataFrame:
    out = frame.copy()
    if len(out) == 0:
        return out
    for recipe in recipes:
        recipe_id = str(recipe.recipe_id)
        ce_col = f"{recipe_id}__ce_net_return"
        pe_col = f"{recipe_id}__pe_net_return"
        if ce_col not in out.columns or pe_col not in out.columns:
            continue
        out[f"{recipe_id}__selected_return"] = np.where(
            out["selected_side"].eq("CE"),
            pd.to_numeric(out[ce_col], errors="coerce").fillna(0.0),
            pd.to_numeric(out[pe_col], errors="coerce").fillna(0.0),
        )
    return out


def _candidate_summary(
    frame: pd.DataFrame,
    *,
    rows_total: int,
    recipe_id: str,
    score_floor: float,
    fraction: float,
) -> dict[str, Any]:
    subset = frame.loc[pd.to_numeric(frame["ranking_score"], errors="coerce").fillna(float("-inf")) >= float(score_floor)].copy()
    sides = subset["selected_side"].astype(str).tolist() if len(subset) else []
    return_col = f"{recipe_id}__selected_return"
    if return_col not in subset.columns:
        raise ValueError(f"missing fixed recipe return column for recipe_id={recipe_id}")
    summary = _summary_with_recipe(
        subset[return_col].astype(float).tolist() if len(subset) else [],
        rows_total=rows_total,
        sides=sides,
        recipe_id=recipe_id,
    )
    summary["fraction"] = float(fraction)
    summary["score_floor"] = float(score_floor)
    return summary


def _oracle_summary(frame: pd.DataFrame, *, rows_total: int, score_floor: float, fraction: float) -> dict[str, Any]:
    subset = frame.loc[pd.to_numeric(frame["ranking_score"], errors="coerce").fillna(float("-inf")) >= float(score_floor)].copy()
    sides = subset["selected_side"].astype(str).tolist() if len(subset) else []
    summary = _summary_with_recipe(
        subset["oracle_selected_side_return"].astype(float).tolist() if len(subset) else [],
        rows_total=rows_total,
        sides=sides,
        recipe_id="ORACLE_SELECTED_SIDE",
    )
    summary["fraction"] = float(fraction)
    summary["score_floor"] = float(score_floor)
    return summary


def run_stage12_confidence_execution(
    *,
    run_dir: str | Path,
    top_fractions: Sequence[float] = DEFAULT_CONFIDENCE_EXECUTION_TOP_FRACTIONS,
    fixed_recipe_ids: Sequence[str] = DEFAULT_CONFIDENCE_EXECUTION_FIXED_RECIPE_IDS,
    validation_policy: Dict[str, Any] | None = None,
    output_root: str | Path | None = None,
) -> Dict[str, Any]:
    source_run_dir = Path(run_dir).resolve()
    summary = _load_json(source_run_dir / "summary.json")
    resolved_config = _load_json(source_run_dir / "resolved_config.json")
    if str(summary.get("status") or "").strip().lower() != "completed":
        raise ValueError(f"run is not completed: {source_run_dir}")

    normalized_fractions = _normalize_top_fractions(top_fractions)
    normalized_fixed_recipe_ids = [str(recipe_id).strip() for recipe_id in fixed_recipe_ids if str(recipe_id).strip()]
    if not normalized_fixed_recipe_ids:
        raise ValueError("fixed_recipe_ids must not be empty")
    policy_config = {
        **DEFAULT_CONFIDENCE_EXECUTION_POLICY,
        **dict(validation_policy or {}),
    }

    parquet_root = Path(str((resolved_config.get("inputs") or {}).get("parquet_root") or "")).resolve()
    support_dataset = str((resolved_config.get("inputs") or {}).get("support_dataset") or "")
    runtime_block_expiry = bool((resolved_config.get("runtime") or {}).get("block_expiry", False))
    support_raw = _load_dataset(parquet_root, support_dataset)
    support_context = support_raw.loc[:, ~support_raw.columns.duplicated()].copy()
    support_filtered, _ = _apply_runtime_filters(
        support_raw,
        block_expiry=runtime_block_expiry,
        context=f"confidence execution support dataset {support_dataset}",
    )
    recipe_universe = _resolve_recipe_universe(
        run_recipe_catalog_id=str(summary.get("recipe_catalog_id") or ""),
        fixed_recipe_ids=normalized_fixed_recipe_ids,
    )
    _, utility = _build_oracle_targets(
        support_filtered,
        recipe_universe,
        cost_per_trade=float(((resolved_config.get("training") or {}).get("cost_per_trade") or 0.0)),
    )
    utility_valid = _window(utility, dict((resolved_config.get("windows") or {}).get("research_valid") or {}))
    utility_holdout = _window(utility, dict((resolved_config.get("windows") or {}).get("final_holdout") or {}))

    stage_artifacts = dict(summary.get("stage_artifacts") or {})
    stage1_package = joblib.load(str(((stage_artifacts.get("stage1") or {}).get("model_package_path")) or ""))
    stage2_package = joblib.load(str(((stage_artifacts.get("stage2") or {}).get("model_package_path")) or ""))
    if not isinstance(stage1_package, dict) or not isinstance(stage2_package, dict):
        raise ValueError("stage1/stage2 model packages must be dictionaries")

    selected_valid = _attach_recipe_selected_returns(
        _selected_stage12_trades_for_window(
            resolved_config=resolved_config,
            summary=summary,
            window_name="research_valid",
            utility_window=utility_valid,
            support_context=support_context,
            parquet_root=parquet_root,
            runtime_block_expiry=runtime_block_expiry,
            stage1_package=stage1_package,
            stage2_package=stage2_package,
        ),
        recipe_universe,
    )
    selected_holdout = _attach_recipe_selected_returns(
        _selected_stage12_trades_for_window(
            resolved_config=resolved_config,
            summary=summary,
            window_name="final_holdout",
            utility_window=utility_holdout,
            support_context=support_context,
            parquet_root=parquet_root,
            runtime_block_expiry=runtime_block_expiry,
            stage1_package=stage1_package,
            stage2_package=stage2_package,
        ),
        recipe_universe,
    )

    if len(selected_valid) == 0:
        raise ValueError("no validation trades were selected after Stage1+Stage2 policy")

    rows: list[dict[str, Any]] = []
    valid_rows_total = int(len(utility_valid))
    holdout_rows_total = int(len(utility_holdout))
    for fraction in normalized_fractions:
        keep_count = _candidate_keep_count(len(selected_valid), fraction)
        score_floor = _safe_float(selected_valid["ranking_score"].iloc[keep_count - 1], default=float("-inf"))
        oracle_valid = _oracle_summary(selected_valid, rows_total=valid_rows_total, score_floor=score_floor, fraction=fraction)
        oracle_holdout = _oracle_summary(selected_holdout, rows_total=holdout_rows_total, score_floor=score_floor, fraction=fraction)
        for recipe_id in normalized_fixed_recipe_ids:
            valid_summary = _apply_policy_soft_preferences(
                _candidate_summary(
                    selected_valid,
                    rows_total=valid_rows_total,
                    recipe_id=recipe_id,
                    score_floor=score_floor,
                    fraction=fraction,
                ),
                policy_config,
            )
            holdout_summary = _apply_policy_soft_preferences(
                _candidate_summary(
                    selected_holdout,
                    rows_total=holdout_rows_total,
                    recipe_id=recipe_id,
                    score_floor=score_floor,
                    fraction=fraction,
                ),
                policy_config,
            )
            rows.append(
                {
                    "recipe_id": str(recipe_id),
                    "fraction": float(fraction),
                    "score_floor": float(score_floor),
                    "validation": valid_summary,
                    "holdout": holdout_summary,
                    "oracle_validation": oracle_valid,
                    "oracle_holdout": oracle_holdout,
                }
            )

    best = max(
        rows,
        key=lambda row: _economic_balance_rank(
            dict(row["validation"]),
            policy_config,
            float(row["validation"]["net_return_sum"]),
            float(row["validation"]["profit_factor"]),
            float(row["fraction"]),
        ),
    )

    analysis_root = Path(output_root).resolve() if output_root is not None else (source_run_dir / "analysis" / "stage12_confidence_execution")
    analysis_root.mkdir(parents=True, exist_ok=True)
    valid_ranked_path = analysis_root / "ranked_trades_valid.parquet"
    holdout_ranked_path = analysis_root / "ranked_trades_holdout.parquet"
    summary_output_path = analysis_root / "execution_summary.json"

    preview_columns = [
        "trade_date",
        "timestamp",
        "snapshot_id",
        "selected_side",
        "entry_prob",
        "direction_trade_prob",
        "direction_up_prob",
        "selected_side_prob",
        "ranking_score",
        "oracle_selected_side_return",
        *[f"{recipe_id}__selected_return" for recipe_id in normalized_fixed_recipe_ids if f"{recipe_id}__selected_return" in selected_valid.columns],
    ]
    selected_valid.loc[:, [column for column in preview_columns if column in selected_valid.columns]].to_parquet(valid_ranked_path, index=False)
    selected_holdout.loc[:, [column for column in preview_columns if column in selected_holdout.columns]].to_parquet(holdout_ranked_path, index=False)

    payload = {
        "analysis_kind": "stage12_confidence_execution_v1",
        "created_at_utc": utc_now(),
        "source_run_dir": str(source_run_dir),
        "source_run_id": str(summary.get("run_id") or source_run_dir.name),
        "ranking": {
            "score_id": "entry_prob_x_trade_gate_prob_x_selected_side_prob_v1",
            "top_fractions": normalized_fractions,
        },
        "validation_policy": dict(policy_config),
        "selected_trade_count": {
            "research_valid": int(len(selected_valid)),
            "final_holdout": int(len(selected_holdout)),
        },
        "fixed_recipe_ids": normalized_fixed_recipe_ids,
        "recipe_universe_recipe_ids": [str(recipe.recipe_id) for recipe in recipe_universe],
        "winner": best,
        "rows": rows,
        "paths": {
            "analysis_root": str(analysis_root),
            "ranked_trades_valid": str(valid_ranked_path),
            "ranked_trades_holdout": str(holdout_ranked_path),
            "execution_summary": str(summary_output_path),
        },
    }
    summary_output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


__all__ = [
    "DEFAULT_CONFIDENCE_EXECUTION_FIXED_RECIPE_IDS",
    "DEFAULT_CONFIDENCE_EXECUTION_POLICY",
    "DEFAULT_CONFIDENCE_EXECUTION_TOP_FRACTIONS",
    "run_stage12_confidence_execution",
]
