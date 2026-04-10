from __future__ import annotations

import json
from itertools import product
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

import joblib
import numpy as np
import pandas as pd

from ..experiment_control.state import utc_now
from .confidence_execution import _attach_recipe_selected_returns, _candidate_summary, _oracle_summary
from .counterfactual import _load_json, _resolve_recipe_universe
from .pipeline import (
    _apply_policy_soft_preferences,
    _apply_runtime_filters,
    _build_oracle_targets,
    _economic_balance_rank,
    _load_dataset,
    _merge_policy_inputs,
    _score_single_target,
    _score_stage2_package,
    _stage2_side_masks_from_policy,
    _window,
)
from .registries import view_registry
from .skew_diagnostic import _drop_base_overlap, _model_level_summary


DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS: tuple[str, ...] = ("L3", "L6")
DEFAULT_STAGE2_CALIBRATION_POLICY: dict[str, Any] = {
    "validation_min_trades_soft": 50,
    "side_share_min": 0.30,
    "side_share_max": 0.70,
    "prefer_non_negative_returns": True,
    "prefer_profit_factor_min": 1.0,
}


def _normalize_grid(values: Iterable[float], *, minimum: float = 0.0, maximum: float = 1.0) -> list[float]:
    seen: set[float] = set()
    normalized: list[float] = []
    for raw in values:
        value = round(float(raw), 6)
        if not np.isfinite(value) or value < minimum or value > maximum:
            raise ValueError(f"grid values must be finite and within [{minimum}, {maximum}]")
        if value in seen:
            continue
        seen.add(value)
        normalized.append(float(value))
    if not normalized:
        raise ValueError("grid must not be empty")
    return sorted(normalized)


def _centered_grid(value: float, *, step: float = 0.05, minimum: float = 0.0, maximum: float = 1.0) -> list[float]:
    candidates = [value - step, value, value + step]
    clipped = [min(maximum, max(minimum, float(v))) for v in candidates]
    return _normalize_grid(clipped, minimum=minimum, maximum=maximum)


def _candidate_stage2_policy(current_policy_id: str, *, trade_threshold: float | None, ce_threshold: float, pe_threshold: float, min_edge: float) -> dict[str, Any]:
    out = {
        "policy_id": str(current_policy_id),
        "selected_ce_threshold": float(ce_threshold),
        "selected_pe_threshold": float(pe_threshold),
        "selected_min_edge": float(min_edge),
    }
    if current_policy_id in {"direction_gate_threshold_v1", "direction_gate_economic_balance_v1"}:
        out["selected_trade_threshold"] = float(trade_threshold if trade_threshold is not None else 0.0)
    return out


def _stage2_selected_frame(
    merged: pd.DataFrame,
    *,
    entry_threshold: float,
    stage2_policy: Dict[str, Any],
    recipe_universe: Sequence[Any],
) -> pd.DataFrame:
    direction_available = pd.to_numeric(
        merged["direction_up_prob"] if "direction_up_prob" in merged.columns else pd.Series([float("nan")] * len(merged), index=merged.index),
        errors="coerce",
    ).notna().to_numpy(dtype=bool, copy=False)
    ce_mask, pe_mask = _stage2_side_masks_from_policy(
        merged,
        entry_threshold=float(entry_threshold),
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
    selected["direction_trade_prob"] = pd.to_numeric(
        selected["direction_trade_prob"] if "direction_trade_prob" in selected.columns else pd.Series([1.0] * len(selected), index=selected.index),
        errors="coerce",
    ).fillna(1.0)
    selected["direction_up_prob"] = pd.to_numeric(selected["direction_up_prob"], errors="coerce").fillna(0.5)
    selected["selected_side_prob"] = np.where(
        selected["selected_side"].eq("CE"),
        selected["direction_up_prob"],
        1.0 - selected["direction_up_prob"],
    )
    selected["ranking_score"] = selected["entry_prob"] * selected["direction_trade_prob"] * selected["selected_side_prob"]
    selected["oracle_selected_side_return"] = np.where(
        selected["selected_side"].eq("CE"),
        pd.to_numeric(selected["best_ce_net_return_after_cost"], errors="coerce").fillna(0.0),
        pd.to_numeric(selected["best_pe_net_return_after_cost"], errors="coerce").fillna(0.0),
    )
    selected = _attach_recipe_selected_returns(selected, recipe_universe)
    return selected.sort_values(
        by=["ranking_score", "selected_side_prob", "entry_prob", "timestamp"],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)


def _evaluate_window(
    selected: pd.DataFrame,
    *,
    rows_total: int,
    fixed_recipe_ids: Sequence[str],
    policy_config: Dict[str, Any],
) -> dict[str, Any]:
    actionable_ce = int(selected["selected_side"].eq("CE").sum()) if "selected_side" in selected.columns else 0
    actionable_pe = int(selected["selected_side"].eq("PE").sum()) if "selected_side" in selected.columns else 0
    actionable = _model_level_summary(selected, rows_total, actionable_ce, actionable_pe)
    oracle_summary = _oracle_summary(selected, rows_total=rows_total, fraction=1.0)
    fixed_recipe_summaries: dict[str, Any] = {}
    for recipe_id in fixed_recipe_ids:
        fixed_recipe_summaries[str(recipe_id)] = _apply_policy_soft_preferences(
            _candidate_summary(selected, rows_total=rows_total, recipe_id=str(recipe_id), fraction=1.0),
            policy_config,
        )
    return {
        "actionable": actionable,
        "oracle_selected_side": oracle_summary,
        "fixed_recipe_summaries": fixed_recipe_summaries,
    }


def _side_gap(actionable: Dict[str, Any]) -> float:
    selected_ce = ((actionable.get("selected_direction") or {}).get("ce_pct"))
    oracle_ce = ((actionable.get("oracle_direction_for_selected") or {}).get("ce_pct"))
    if selected_ce is None or oracle_ce is None:
        return 1.0
    return abs(float(selected_ce) - float(oracle_ce))


def _candidate_rank(validation_eval: Dict[str, Any], *, selected_recipe_id: str, policy_config: Dict[str, Any]) -> tuple[float, ...]:
    recipe_summary = dict((validation_eval.get("fixed_recipe_summaries") or {}).get(str(selected_recipe_id)) or {})
    actionable = dict(validation_eval.get("actionable") or {})
    agreement = float(actionable.get("selected_vs_oracle_agreement") or 0.0)
    return _economic_balance_rank(
        recipe_summary,
        policy_config,
        -_side_gap(actionable),
        agreement,
    )


def _best_recipe_id(window_eval: Dict[str, Any], *, policy_config: Dict[str, Any]) -> str:
    summaries = dict(window_eval.get("fixed_recipe_summaries") or {})
    if not summaries:
        raise ValueError("fixed recipe summaries must not be empty")
    return max(
        summaries.keys(),
        key=lambda recipe_id: _economic_balance_rank(dict(summaries[recipe_id]), policy_config),
    )


def _build_window_frame(
    *,
    resolved_config: Dict[str, Any],
    summary: Dict[str, Any],
    window_name: str,
    diagnostic_window: pd.DataFrame,
    support_context: pd.DataFrame,
    parquet_root: Path,
    runtime_block_expiry: bool,
    stage1_package: Dict[str, Any],
    stage2_package: Dict[str, Any],
) -> pd.DataFrame:
    component_ids = dict(summary.get("component_ids") or {})
    stage1_view_id = str(((component_ids.get("stage1") or {}).get("view_id")) or "")
    stage2_view_id = str(((component_ids.get("stage2") or {}).get("view_id")) or "")
    stage1_dataset = view_registry()[stage1_view_id].dataset_name
    stage2_dataset = view_registry()[stage2_view_id].dataset_name

    stage1_raw = _load_dataset(parquet_root, stage1_dataset)
    stage2_raw = _load_dataset(parquet_root, stage2_dataset)
    stage1_filtered, _ = _apply_runtime_filters(
        stage1_raw,
        block_expiry=runtime_block_expiry,
        support_context=support_context,
        context=f"stage2 calibration {window_name} stage1",
    )
    stage2_filtered, _ = _apply_runtime_filters(
        stage2_raw,
        block_expiry=runtime_block_expiry,
        support_context=support_context,
        context=f"stage2 calibration {window_name} stage2",
    )
    window_cfg = dict((resolved_config.get("windows") or {}).get(window_name) or {})
    stage1_window = _window(stage1_filtered, window_cfg)
    stage2_window = _window(stage2_filtered, window_cfg)
    stage1_scores = _drop_base_overlap(
        _score_single_target(stage1_window, stage1_package, prob_col="entry_prob"),
        diagnostic_window.columns,
    )
    stage2_scores = _drop_base_overlap(
        _score_stage2_package(stage2_window, stage2_package),
        diagnostic_window.columns,
    )
    return _merge_policy_inputs(diagnostic_window, stage1_scores, stage2_scores)


def run_stage2_calibration_diagnostic(
    *,
    run_dir: str | Path,
    fixed_recipe_ids: Sequence[str] = DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS,
    trade_threshold_grid: Sequence[float] | None = None,
    ce_threshold_grid: Sequence[float] | None = None,
    pe_threshold_grid: Sequence[float] | None = None,
    min_edge_grid: Sequence[float] | None = None,
    validation_policy: Dict[str, Any] | None = None,
    output_root: str | Path | None = None,
) -> Dict[str, Any]:
    source_run_dir = Path(run_dir).resolve()
    summary = _load_json(source_run_dir / "summary.json")
    resolved_config = _load_json(source_run_dir / "resolved_config.json")
    if str(summary.get("status") or "").strip().lower() != "completed":
        raise ValueError(f"run is not completed: {source_run_dir}")

    policy_config = {**DEFAULT_STAGE2_CALIBRATION_POLICY, **dict(validation_policy or {})}
    normalized_fixed_recipe_ids = [str(recipe_id).strip() for recipe_id in fixed_recipe_ids if str(recipe_id).strip()]
    if not normalized_fixed_recipe_ids:
        raise ValueError("fixed_recipe_ids must not be empty")

    parquet_root = Path(str((resolved_config.get("inputs") or {}).get("parquet_root") or "")).resolve()
    support_dataset = str((resolved_config.get("inputs") or {}).get("support_dataset") or "")
    runtime_block_expiry = bool((resolved_config.get("runtime") or {}).get("block_expiry", False))
    support_raw = _load_dataset(parquet_root, support_dataset)
    support_context = support_raw.loc[:, ~support_raw.columns.duplicated()].copy()
    support_filtered, _ = _apply_runtime_filters(
        support_raw,
        block_expiry=runtime_block_expiry,
        context=f"stage2 calibration support dataset {support_dataset}",
    )

    recipe_universe = _resolve_recipe_universe(
        run_recipe_catalog_id=str(summary.get("recipe_catalog_id") or ""),
        fixed_recipe_ids=normalized_fixed_recipe_ids,
    )
    oracle, utility = _build_oracle_targets(
        support_filtered,
        recipe_universe,
        cost_per_trade=float(((resolved_config.get("training") or {}).get("cost_per_trade") or 0.0)),
    )
    utility_dupes = [c for c in utility.columns if c in set(oracle.columns) - {"trade_date", "timestamp", "snapshot_id"}]
    utility_base = utility.drop(columns=utility_dupes) if utility_dupes else utility
    diagnostic_base = _merge_policy_inputs(oracle, utility_base)
    diagnostic_valid = _window(diagnostic_base, dict((resolved_config.get("windows") or {}).get("research_valid") or {}))
    diagnostic_holdout = _window(diagnostic_base, dict((resolved_config.get("windows") or {}).get("final_holdout") or {}))

    stage_artifacts = dict(summary.get("stage_artifacts") or {})
    stage1_package = joblib.load(str(((stage_artifacts.get("stage1") or {}).get("model_package_path")) or ""))
    stage2_package = joblib.load(str(((stage_artifacts.get("stage2") or {}).get("model_package_path")) or ""))

    merged_valid = _build_window_frame(
        resolved_config=resolved_config,
        summary=summary,
        window_name="research_valid",
        diagnostic_window=diagnostic_valid,
        support_context=support_context,
        parquet_root=parquet_root,
        runtime_block_expiry=runtime_block_expiry,
        stage1_package=stage1_package,
        stage2_package=stage2_package,
    )
    merged_holdout = _build_window_frame(
        resolved_config=resolved_config,
        summary=summary,
        window_name="final_holdout",
        diagnostic_window=diagnostic_holdout,
        support_context=support_context,
        parquet_root=parquet_root,
        runtime_block_expiry=runtime_block_expiry,
        stage1_package=stage1_package,
        stage2_package=stage2_package,
    )

    stage1_policy = dict((summary.get("policy_reports") or {}).get("stage1") or {})
    stage2_policy = dict((summary.get("policy_reports") or {}).get("stage2") or {})
    current_policy_id = str(stage2_policy.get("policy_id") or "direction_gate_threshold_v1")
    entry_threshold = float(stage1_policy["selected_threshold"])

    trade_grid = (
        _normalize_grid(trade_threshold_grid, minimum=0.0, maximum=1.0)
        if trade_threshold_grid is not None
        else _centered_grid(float(stage2_policy.get("selected_trade_threshold", 0.5)))
    )
    ce_grid = (
        _normalize_grid(ce_threshold_grid, minimum=0.0, maximum=1.0)
        if ce_threshold_grid is not None
        else _centered_grid(float(stage2_policy.get("selected_ce_threshold", 0.5)))
    )
    pe_grid = (
        _normalize_grid(pe_threshold_grid, minimum=0.0, maximum=1.0)
        if pe_threshold_grid is not None
        else _centered_grid(float(stage2_policy.get("selected_pe_threshold", 0.5)))
    )
    edge_grid = (
        _normalize_grid(min_edge_grid, minimum=0.0, maximum=1.0)
        if min_edge_grid is not None
        else _centered_grid(float(stage2_policy.get("selected_min_edge", 0.0)), minimum=0.0, maximum=1.0)
    )

    uses_trade_gate = current_policy_id in {"direction_gate_threshold_v1", "direction_gate_economic_balance_v1"}
    trade_values = trade_grid if uses_trade_gate else [None]

    rows: list[dict[str, Any]] = []
    for trade_threshold, ce_threshold, pe_threshold, min_edge in product(trade_values, ce_grid, pe_grid, edge_grid):
        candidate_policy = _candidate_stage2_policy(
            current_policy_id,
            trade_threshold=trade_threshold,
            ce_threshold=ce_threshold,
            pe_threshold=pe_threshold,
            min_edge=min_edge,
        )
        selected_valid = _stage2_selected_frame(
            merged_valid,
            entry_threshold=entry_threshold,
            stage2_policy=candidate_policy,
            recipe_universe=recipe_universe,
        )
        selected_holdout = _stage2_selected_frame(
            merged_holdout,
            entry_threshold=entry_threshold,
            stage2_policy=candidate_policy,
            recipe_universe=recipe_universe,
        )
        validation_eval = _evaluate_window(
            selected_valid,
            rows_total=int(len(diagnostic_valid)),
            fixed_recipe_ids=normalized_fixed_recipe_ids,
            policy_config=policy_config,
        )
        holdout_eval = _evaluate_window(
            selected_holdout,
            rows_total=int(len(diagnostic_holdout)),
            fixed_recipe_ids=normalized_fixed_recipe_ids,
            policy_config=policy_config,
        )
        selected_recipe_id = _best_recipe_id(validation_eval, policy_config=policy_config)
        rows.append(
            {
                "policy_id": current_policy_id,
                "trade_threshold": None if trade_threshold is None else float(trade_threshold),
                "ce_threshold": float(ce_threshold),
                "pe_threshold": float(pe_threshold),
                "min_edge": float(min_edge),
                "is_current_policy": (
                    abs(float(stage2_policy.get("selected_ce_threshold", -1.0)) - float(ce_threshold)) < 1e-9
                    and abs(float(stage2_policy.get("selected_pe_threshold", -1.0)) - float(pe_threshold)) < 1e-9
                    and abs(float(stage2_policy.get("selected_min_edge", -1.0)) - float(min_edge)) < 1e-9
                    and (
                        not uses_trade_gate
                        or abs(float(stage2_policy.get("selected_trade_threshold", -1.0)) - float(trade_threshold or 0.0)) < 1e-9
                    )
                ),
                "validation_selected_recipe_id": str(selected_recipe_id),
                "validation": validation_eval,
                "holdout": holdout_eval,
            }
        )

    winner = max(
        rows,
        key=lambda row: _candidate_rank(
            dict(row["validation"]),
            selected_recipe_id=str(row["validation_selected_recipe_id"]),
            policy_config=policy_config,
        ),
    )

    analysis_root = Path(output_root).resolve() if output_root is not None else (source_run_dir / "analysis" / "stage2_calibration_diagnostic")
    analysis_root.mkdir(parents=True, exist_ok=True)
    summary_output_path = analysis_root / "stage2_calibration_summary.json"

    payload = {
        "analysis_kind": "stage2_calibration_diagnostic_v1",
        "created_at_utc": utc_now(),
        "source_run_dir": str(source_run_dir),
        "source_run_id": str(summary.get("run_id") or source_run_dir.name),
        "stage1_entry_threshold": entry_threshold,
        "current_stage2_policy": dict(stage2_policy),
        "grid": {
            "policy_id": current_policy_id,
            "trade_threshold_grid": trade_grid if uses_trade_gate else [],
            "ce_threshold_grid": ce_grid,
            "pe_threshold_grid": pe_grid,
            "min_edge_grid": edge_grid,
        },
        "validation_policy": dict(policy_config),
        "fixed_recipe_ids": normalized_fixed_recipe_ids,
        "recipe_universe_recipe_ids": [str(recipe.recipe_id) for recipe in recipe_universe],
        "winner": winner,
        "rows": rows,
        "paths": {
            "analysis_root": str(analysis_root),
            "stage2_calibration_summary": str(summary_output_path),
        },
    }
    summary_output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


__all__ = [
    "DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS",
    "DEFAULT_STAGE2_CALIBRATION_POLICY",
    "run_stage2_calibration_diagnostic",
]
